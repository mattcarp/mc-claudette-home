type Fields = Record<string, unknown>;

function fmt(level: string, msg: string, fields: Fields): string {
  const parts: string[] = [
    new Date().toISOString(),
    level.padEnd(5),
    msg,
  ];
  for (const [k, v] of Object.entries(fields)) {
    if (v === undefined || v === null) continue;
    const s = typeof v === "string" ? v : JSON.stringify(v);
    parts.push(`${k}=${s}`);
  }
  return parts.join(" ");
}

export interface Logger {
  info(msg: string, fields?: Fields): void;
  warn(msg: string, fields?: Fields): void;
  error(msg: string, fields?: Fields): void;
  debug(msg: string, fields?: Fields): void;
  child(fields: Fields): Logger;
}

function make(base: Fields): Logger {
  return {
    info: (msg, fields = {}) => console.log(fmt("INFO", msg, { ...base, ...fields })),
    warn: (msg, fields = {}) => console.warn(fmt("WARN", msg, { ...base, ...fields })),
    error: (msg, fields = {}) => console.error(fmt("ERROR", msg, { ...base, ...fields })),
    debug: (msg, fields = {}) => {
      if (process.env.SYMPHONY_DEBUG) {
        console.log(fmt("DEBUG", msg, { ...base, ...fields }));
      }
    },
    child: (fields) => make({ ...base, ...fields }),
  };
}

export const logger: Logger = make({});
