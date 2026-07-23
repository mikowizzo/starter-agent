/** Pure SSE stream reader.
 *
 * Reads an SSE stream from a fetch Response body, parses events,
 * and invokes onEvent for each one.
 */

export async function readSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (d: any, eventType: string) => void,
): Promise<void> {
  const dec = new TextDecoder();
  let buf = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });

    const parts = buf.split("\n\n");
    buf = parts.pop()!;

    for (const part of parts) {
      let eventType = "";
      let data = "";
      for (const line of part.split("\n")) {
        if (line.startsWith("event: ")) eventType = line.slice(7);
        if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (!data || eventType === "heartbeat") continue;

      let d: any;
      try {
        d = JSON.parse(data);
      } catch {
        continue;
      }

      // Resume metadata events — skip them
      if (["replay", "catch_up", "subscribed"].includes(eventType))
        continue;

      onEvent(d, eventType);
    }
  }
}
