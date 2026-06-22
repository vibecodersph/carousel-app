import { readFile } from "node:fs/promises";

export async function loadDotEnv(path = new URL("../.env", import.meta.url).pathname): Promise<void> {
  let text = "";
  try {
    text = await readFile(path, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw error;
  }

  for (const rawLine of text.split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    if (line.startsWith("export ")) line = line.slice("export ".length).trim();
    const index = line.indexOf("=");
    const key = line.slice(0, index).trim();
    let value = line.slice(index + 1).trim();
    const quote = value[0];
    if (quote === "'" || quote === "\"") {
      const end = value.indexOf(quote, 1);
      value = end > 0 ? value.slice(1, end) : value.slice(1);
    } else {
      value = value.replace(/\s+#.*$/, "").trim();
    }
    if (key && process.env[key] === undefined) {
      process.env[key] = value;
    }
  }
}
