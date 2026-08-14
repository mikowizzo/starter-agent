/** Pure SSE stream reader.
 *
 * Reads an SSE stream from a fetch Response body, parses events,
 * and invokes onEvent for each one.
 *
 * Also watches for a stalled stream: agno sends heartbeat comments every
 * ~30s on live runs, so if NO bytes at all arrive for stallTimeoutMs the
 * connection is dead (proxy silently dropped it, NAT timeout, etc.) even
 * though reader.read() never rejects. We cancel the reader and throw
 * StreamTimeoutError so callers can run their resume flow.
 */

export class StreamTimeoutError extends Error {
  constructor() {
    super("Stream stalled — no bytes received");
    this.name = "StreamTimeoutError";
  }
}

export async function readSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (d: any, eventType: string) => void,
  stallTimeoutMs = 90_000,
): Promise<void> {
  const dec = new TextDecoder();
  let buf = "";

  while (true) {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      // Any bytes (events, heartbeats, comments) reset the stall clock.
      const result = await Promise.race([
        reader.read(),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new StreamTimeoutError()), stallTimeoutMs);
        }),
      ]);

      const { done, value } = result;
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
    } catch (err) {
      if (err instanceof StreamTimeoutError) {
        // Best-effort close so the socket doesn't linger.
        reader.cancel().catch(() => {});
      }
      throw err;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}
