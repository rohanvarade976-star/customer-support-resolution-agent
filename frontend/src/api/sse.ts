// Server-Sent Events over fetch(). The browser's EventSource cannot send an Authorization
// header, and we never put a token in a URL (ADR-019), so we read the stream ourselves.
//
// The format: events are separated by a blank line. Each line is "field: value". "event"
// names the event, "data" holds the payload (several data lines are joined by "\n"), "id"
// names it. Lines starting with ":" are comments (keep-alives) and are ignored.

export interface SseMessage {
  event: string;
  data: string;
  id: string | null;
}

export class SseParser {
  private buffer = "";

  // Feed raw text; get back every event completed by it. Partial events wait for more text.
  push(text: string): SseMessage[] {
    this.buffer += text.replace(/\r\n?/g, "\n");
    const blocks = this.buffer.split("\n\n");
    this.buffer = blocks.pop() ?? ""; // the last piece is incomplete (or empty)
    return blocks.map(parseBlock).filter((message) => message !== null);
  }
}

function parseBlock(block: string): SseMessage | null {
  let event = "message";
  let id: string | null = null;
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    else if (field === "id") id = value;
  }
  return data.length === 0 ? null : { event, data: data.join("\n"), id };
}

// Reads a streaming response to its end, calling `onMessage` for each event.
export async function readSse(
  response: Response,
  onMessage: (message: SseMessage) => void,
): Promise<void> {
  if (response.body === null) throw new Error("The response has no body to stream.");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SseParser();
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    for (const message of parser.push(value)) onMessage(message);
  }
}
