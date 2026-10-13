/* Dependency-free behavior tests; native dialog accessibility needs browser QA. */
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {test} = require("node:test");
const {runInNewContext} = require("node:vm");
const markdownit = require("../src/assistant_agent/static/vendor/markdown-it-14.1.0.min.js");
const source = readFileSync(join(__dirname, "../src/assistant_agent/static/file-viewer.js"), "utf8");

// Only model APIs used by this script. Fetch deliberately resolves after abort to
// verify response guards even if cancellation arrives too late for the transport.
class Element {
  constructor(tag, connected = false) {
    this.tagName = tag;
    this.children = [];
    this.listeners = new Map();
    this.connected = connected;
    this.className = "";
    this.classList = {add: (...names) => { this.className += ` ${names.join(" ")}`; }};
    this._open = false;
    this._text = "";
  }
  get isConnected() { return this.connected || Boolean(this.parent?.isConnected); }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.replaceChildren(); this._text = value; }
  append(...children) {
    for (const child of children) { child.parent = this; this.children.push(child); }
  }
  replaceChildren(...children) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this._text = "";
    this.append(...children);
  }
  remove() {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
  addEventListener(type, handler) { this.listeners.set(type, handler); }
  emit(type) { return this.listeners.get(type)?.(); }
  get open() { return this._open; }
  set open(value) {
    if (this._open === value) return;
    this._open = value;
    if (this.tagName === "details") queueMicrotask(() => this.emit("toggle"));
  }
  showModal() { this.open = true; }
  close() { this.open = false; this.emit("close"); }
  focus() { this.focused = true; }
}
const settle = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const nodes = new Map();
  const pending = [];
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, new Element(id === "file-preview" ? "dialog" : "div", true));
      return nodes.get(id);
    },
    createElement: tag => new Element(tag),
  };
  const window = {markdownit};
  runInNewContext(source, {window, document, AbortController, URLSearchParams,
    fetch: (url, options) => new Promise(resolve => pending.push({
      url: new URL(url, "https://app.example"), signal: options.signal,
      respond: (data, ok = true) => resolve({ok, json: async () => data}),
    })),
  });
  return {nodes, pending, viewer: window.fileViewer, tree: nodes.get("files-tree"), body: nodes.get("file-preview-body"), dialog: nodes.get("file-preview")};
}
const entries = ["one.txt", "two.txt"].map(name => ({name, path: name, kind: "file"}));
async function loadedFiles() {
  const app = harness();
  app.viewer.setConversation("conversation-one", true);
  app.tree.children[0].children[0].open = true;
  await settle();
  app.pending.shift().respond({entries, next_offset: null});
  await settle();
  app.buttons = app.tree.children[0].children[0].children[1].children.map(item => item.children[0]);
  return app;
}
const preview = (content, extra = {}) => ({format: "text", content, size: 42, modified: "2026-10-12T00:00:00Z", ...extra});

test("availability gates browsing and unchanged snapshots do not refresh", async () => {
  const app = harness();
  app.viewer.setConversation("conversation-one", false);
  assert.equal(app.tree.hidden, true);
  assert.equal(app.nodes.get("files-refresh").disabled, true);
  assert.equal(app.pending.length, 0);
  app.viewer.setConversation("conversation-one", true);
  assert.equal(app.tree.children.length, 3);
  const first = app.tree.children[0];
  app.viewer.setConversation("conversation-one", true);
  assert.equal(app.tree.children[0], first);
});

test("manual refresh preserves nested expansion and rejects old directory responses", async () => {
  const app = harness();
  app.viewer.setConversation("conversation-one", true);
  app.tree.children[0].children[0].open = true;
  await settle();
  const parentListing = {entries: [{name: "nested", path: "nested", kind: "directory"}], next_offset: null};
  app.pending.shift().respond(parentListing);
  await settle();
  const rootChildren = app.tree.children[0].children[0].children[1];
  rootChildren.children[0].children[0].open = true;
  await settle();
  const stale = app.pending.shift();
  app.nodes.get("files-refresh").emit("click");
  await settle();
  assert.equal(stale.signal.aborted, true);
  assert.equal(app.tree.children[0].children[0].open, true);
  app.pending.shift().respond(parentListing);
  await settle();
  const nested = app.tree.children[0].children[0].children[1].children[0].children[0];
  assert.equal(nested.open, true);
  assert.equal(app.pending[0].url.searchParams.get("path"), "nested");
  app.pending.shift().respond({entries: [], next_offset: null});
  stale.respond({entries, next_offset: null});
  await settle();
  assert.match(nested.textContent, /Empty folder/);
  assert.doesNotMatch(app.tree.textContent, /one\.txt/);
});

test("new selection and conversation reset ignore stale previews", async () => {
  const app = await loadedFiles();
  app.buttons[0].emit("click");
  const staleSelection = app.pending.shift();
  app.buttons[1].emit("click");
  const current = app.pending.shift();
  assert.equal(staleSelection.signal.aborted, true);
  current.respond(preview("second file"));
  staleSelection.respond(preview("stale first file"));
  await settle();
  assert.equal(app.body.textContent, "second file");
  app.buttons[0].emit("click");
  const staleConversation = app.pending.shift();
  app.viewer.setConversation("conversation-two", false);
  staleConversation.respond(preview("old conversation secret"));
  await settle();
  assert.equal(staleConversation.signal.aborted, true);
  assert.equal(app.dialog.open, false);
  assert.equal(app.body.textContent, "");
  assert.equal(app.tree.children.length, 0);
});

test("close cancels preview and restores trigger focus", async () => {
  const app = await loadedFiles();
  app.buttons[0].emit("click");
  const request = app.pending.shift();
  app.nodes.get("file-preview-close").emit("click");
  request.respond(preview("late content"));
  await settle();
  assert.equal(request.signal.aborted, true);
  assert.equal(app.body.textContent, "");
  assert.equal(app.buttons[0].focused, true);
});

test("binary and truncated previews show metadata and notices", async () => {
  const app = await loadedFiles();
  app.buttons[0].emit("click");
  app.pending.shift().respond(preview(null, {format: "binary"}));
  await settle();
  assert.match(app.body.textContent, /Preview unavailable/);
  assert.match(app.nodes.get("file-preview-meta").textContent, /42 bytes · Modified/);
  app.buttons[1].emit("click");
  app.pending.shift().respond(preview("<script>literal text</script>", {truncated: true}));
  await settle();
  assert.match(app.body.textContent, /truncated to the first 1 MiB/);
  assert.equal(app.body.children[1].tagName, "pre");
  assert.equal(app.body.children[1].textContent, "<script>literal text</script>");
});

test("actual preview renderer escapes HTML, omits images and restricts links", async () => {
  const app = await loadedFiles();
  app.buttons[0].emit("click");
  const content = '<script>alert(1)</script>\n<img src="https://tracker.example/x">\n![pixel](https://tracker.example/y)\n[x](javascript:alert(1))\n[relative](/disconnect)\n[safe](https://example.com)';
  app.pending.shift().respond(preview(content, {format: "markdown"}));
  await settle();
  const html = app.body.children[0].innerHTML;
  assert.doesNotMatch(html, /<script|<img|href="javascript:|href="\/disconnect/);
  assert.match(html, /href="https:\/\/example.com"/);
  assert.match(html, /rel="noopener noreferrer"/);
});
