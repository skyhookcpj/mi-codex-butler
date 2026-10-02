import { Butler } from "./butler.ts";
import { SpawnCodexRunner } from "./codex.ts";
import { configPath, loadConfig, type ButlerConfig } from "./config.ts";
import { log } from "./log.ts";
import { SpeakerLink } from "./speaker.ts";
import { HistoryStore, SessionStore } from "./store.ts";
import { startWeb } from "./web.ts";

let config: ButlerConfig = loadConfig();
const getConfig = () => config;

const speaker = new SpeakerLink(getConfig);
const butler = new Butler({
  config: getConfig,
  runner: new SpawnCodexRunner(),
  sessions: new SessionStore(),
  history: new HistoryStore(),
  say: (text) => void speaker.say(text),
});

speaker.on("utterance", async (id: string, text: string) => {
  const outcome = await butler.handle(text, "speaker");
  if (!speaker.reply({ reply_to: id, kind: outcome.kind, text: outcome.text })) {
    log("warn", "回复时音箱不在线，这句没念出来");
  }
});

log("info", `配置文件 ${configPath()}`);
await speaker.listen(config.wsPort);
await startWeb(config.webPort, {
  butler,
  speaker,
  getConfig,
  setConfig: (next) => (config = next),
});

for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.on(signal, () => {
    butler.cancel();
    void speaker.close().finally(() => process.exit(0));
  });
}
