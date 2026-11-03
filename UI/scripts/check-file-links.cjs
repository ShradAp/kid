// Focused regression checks for the chat renderer, without a browser or backend.
// Run from UI: node scripts/check-file-links.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const cache = new Map();
function load(relative) {
  const filename = path.resolve(__dirname, "../src", relative);
  if (cache.has(filename)) return cache.get(filename);
  const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
    fileName: filename,
  }).outputText;
  const module = { exports: {} };
  const localRequire = (name) => {
    if (name.endsWith(".css")) return {};
    if (name === "@/lib/file-actions") return load("lib/file-actions.ts");
    if (name === "@/components/ui/tooltip") {
      const wrapper = ({ children }) => React.createElement(React.Fragment, null, children);
      const trigger = ({ render, children }) => React.cloneElement(render, {}, children);
      return { Tooltip: wrapper, TooltipContent: wrapper, TooltipTrigger: trigger };
    }
    return require(name);
  };
  new Function("require", "module", "exports", compiled)(localRequire, module, module.exports);
  cache.set(filename, module.exports);
  return module.exports;
}

function anchors(node) {
  if (Array.isArray(node)) return node.flatMap(anchors);
  if (!React.isValidElement(node)) return [];
  return [...(node.type === "a" ? [node] : []), ...anchors(node.props.children)];
}

async function main() {
  const { renderMarkdown } = load("components/chat/message-markup.tsx");
  const actions = load("lib/file-actions.ts");
  const fixtures = [
    ["[TikTok_в_текст.pyw](</C:/Users/Ivan/OneDrive/Рабочий стол/TikTok_в_текст.pyw>)", "/C:/Users/Ivan/OneDrive/Рабочий стол/TikTok_в_текст.pyw"],
    ["[Папка](<C:/Рабочий стол/TikTok_тексты>)", "C:/Рабочий стол/TikTok_тексты"],
    ["[Отчёт](C:/Рабочий стол/Отчёт (1).txt)", "C:/Рабочий стол/Отчёт (1).txt"],
    ["[Файл](file:///C:/Рабочий%20стол/текст.txt)", "file:///C:/Рабочий%20стол/текст.txt"],
    ["[Код](C:\\work\\app.py:12:3)", "C:\\work\\app.py:12:3"],
    ['[Код](<C:/Рабочий стол/app.py> "Описание")', "C:/Рабочий стол/app.py"],
    ['[Код](C:/work/app.py "Описание")', "C:/work/app.py"],
  ];
  const requests = [];
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ ok: true }) };
  };
  for (const [source, expected] of fixtures) {
    const nodes = renderMarkdown(`Готово: ${source} — результат.`);
    const links = anchors(nodes);
    assert.equal(links.length, 1, source);
    assert.equal(links[0].props.href, expected);
    assert.equal(links[0].props.title, "Показать в папке");
    assert.ok(!renderToStaticMarkup(nodes).includes("]("), source);
    let prevented = false;
    links[0].props.onClick({ preventDefault: () => { prevented = true; } });
    assert.ok(prevented);
    assert.equal(requests.at(-1).url, "/api/reveal-local");
    assert.equal(requests.at(-1).options.method, "POST");
    assert.deepEqual(JSON.parse(requests.at(-1).options.body), { path: expected });
  }
  const links = anchors(renderMarkdown("[Сайт](https://example.com/a_(b)) и https://example.org"));
  assert.equal(links.length, 2);
  assert.equal(links[0].props.href, "https://example.com/a_(b)");
  assert.equal(links[0].props.target, "_blank");
  assert.equal(links[0].props.onClick, undefined);
  assert.equal(anchors(renderMarkdown("`[Код](C:/work/app.py)`")).length, 0);
  assert.ok(renderToStaticMarkup(renderMarkdown("[Скачать файл «test.txt»](rab://download/abc123)")).includes("/api/generated/abc123"));
  global.fetch = async () => ({ ok: false, json: async () => ({ error: "Файл не найден" }) });
  await assert.rejects(actions.revealLocalFile("C:/missing.txt"), /Файл не найден/);
  console.log("File link rendering, click handling and error checks passed.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
