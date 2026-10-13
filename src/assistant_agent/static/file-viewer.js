/* Read-only, conversation-scoped sandbox browsing. */
(() => {
  "use strict";
  const tree = document.getElementById("files-tree");
  const status = document.getElementById("files-status");
  const refresh = document.getElementById("files-refresh");
  const dialog = document.getElementById("file-preview");
  const title = document.getElementById("file-preview-title");
  const metadata = document.getElementById("file-preview-meta");
  const body = document.getElementById("file-preview-body");
  const expanded = new Set();
  const directoryRequests = new Set();
  let conversationId = null, available = false, generation = 0;
  let previewRequest = null, previewTrigger = null;
  const markdown = window.markdownit({html: false, linkify: false});
  markdown.disable("image");
  const defaultValidateLink = markdown.validateLink;
  markdown.validateLink = url => /^https?:\/\//i.test(url) && defaultValidateLink(url);
  const defaultLinkOpen = markdown.renderer.rules.link_open;
  markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
    return defaultLinkOpen ? defaultLinkOpen(tokens, index, options, env, self) : self.renderToken(tokens, index, options);
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(text, handler) {
    const node = element("button", "file-action", text);
    node.type = "button";
    node.addEventListener("click", handler);
    return node;
  }
  function abortDirectories() {
    for (const controller of directoryRequests) controller.abort();
    directoryRequests.clear();
  }
  async function request(endpoint, root, path, controller, offset) {
    const query = new URLSearchParams({conversation_id: conversationId, root, path});
    if (offset !== undefined) query.set("offset", offset);
    const result = await fetch(`${endpoint}?${query}`, {signal: controller.signal, cache: "no-store"});
    const data = await result.json();
    if (!result.ok) throw new Error(data.error || "Unable to load files. Please try again.");
    return data;
  }
  function directory(root, path, name) {
    const item = element("li");
    const details = element("details", "file-directory");
    const summary = element("summary", "file-row", name);
    const children = element("ul", "file-children");
    const key = `${root}/${path}`;
    let loaded = false;
    details.append(summary, children);
    item.append(details);
    details.open = expanded.has(key);
    details.addEventListener("toggle", () => {
      if (details.open) {
        expanded.add(key);
        if (!loaded) { loaded = true; loadDirectory(root, path, children); }
      } else expanded.delete(key);
    });
    // The queued toggle event loads restored expanded folders after insertion.
    return item;
  }
  async function loadDirectory(root, path, container, offset = 0) {
    const controller = new AbortController();
    directoryRequests.add(controller);
    const currentGeneration = generation;
    const notice = element("li", "file-notice", "Loading…");
    if (!offset) container.replaceChildren();
    container.append(notice);
    try {
      const data = await request("/api/files", root, path, controller, offset);
      if (controller.signal.aborted || currentGeneration !== generation || !container.isConnected) return;
      notice.remove();
      for (const entry of data.entries) {
        if (entry.kind === "directory") container.append(directory(root, entry.path, entry.name));
        else {
          const item = element("li");
          if (entry.kind === "file") {
            const open = button(entry.name, () => openPreview(root, entry.path, open));
            open.classList.add("file-row", "file-leaf");
            item.append(open);
          } else {
            item.append(element("span", "file-row file-unavailable", `${entry.name} · unavailable`));
          }
          container.append(item);
        }
      }
      if (!data.entries.length && !offset) container.append(element("li", "file-notice", "Empty folder"));
      if (data.next_offset !== null && data.next_offset !== undefined) {
        const more = element("li");
        more.append(button("Load more", () => { more.remove(); loadDirectory(root, path, container, data.next_offset); }));
        container.append(more);
      }
    } catch (error) {
      if (controller.signal.aborted || currentGeneration !== generation || !container.isConnected) return;
      notice.replaceChildren(element("span", "file-error", error.message), button("Retry", () => {
        notice.remove(); loadDirectory(root, path, container, offset);
      }));
    } finally { directoryRequests.delete(controller); }
  }
  function roots() {
    abortDirectories();
    tree.replaceChildren(...["input", "workspace", "daily-logs"].map(root => directory(root, "", `/${root}`)));
  }
  function sizeLabel(size) {
    if (size === null || size === undefined) return "Size unavailable";
    if (size < 1024) return `${size} bytes`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KiB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
  }
  async function openPreview(root, path, trigger) {
    if (previewRequest) previewRequest.abort();
    const controller = new AbortController();
    previewRequest = controller;
    const currentGeneration = generation;
    previewTrigger = trigger;
    title.textContent = `/${root}/${path}`;
    metadata.textContent = "";
    body.replaceChildren(element("p", "lede", "Loading preview…"));
    if (!dialog.open) dialog.showModal();
    try {
      const data = await request("/api/files/preview", root, path, controller);
      if (controller.signal.aborted || currentGeneration !== generation || !dialog.open) return;
      const date = data.modified == null ? null : new Date(typeof data.modified === "number" ? data.modified * 1000 : data.modified);
      metadata.textContent = sizeLabel(data.size) + (date && !Number.isNaN(date.valueOf()) ? ` · Modified ${date.toLocaleString()}` : "");
      body.replaceChildren();
      if (data.truncated) body.append(element("p", "file-notice", "Preview truncated to the first 1 MiB."));
      if (data.format === "binary") body.append(element("p", "lede", "Preview unavailable. This file is not UTF-8 text."));
      else if (data.format === "markdown") {
        const rendered = element("div", "file-markdown");
        rendered.innerHTML = markdown.render(data.content || "");
        body.append(rendered);
      } else body.append(element("pre", "file-source", data.content || ""));
      if (data.format !== "binary" && !data.content) body.append(element("p", "lede", "This file is empty."));
    } catch (error) {
      if (controller.signal.aborted || currentGeneration !== generation || !dialog.open) return;
      body.replaceChildren(element("p", "file-error", error.message), button("Retry", () => openPreview(root, path, trigger)));
    }
  }
  document.getElementById("file-preview-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    if (previewRequest) previewRequest.abort();
    previewRequest = null;
    body.replaceChildren();
    if (previewTrigger && previewTrigger.isConnected) previewTrigger.focus();
  });
  refresh.addEventListener("click", roots);
  window.fileViewer = {
    setConversation(id, isAvailable) {
      const nextAvailable = Boolean(id && isAvailable);
      if (conversationId === id && available === nextAvailable) return;
      generation++;
      abortDirectories();
      if (previewRequest) previewRequest.abort();
      if (dialog.open) dialog.close();
      expanded.clear();
      conversationId = id;
      available = nextAvailable;
      refresh.disabled = !available;
      tree.hidden = !available;
      status.textContent = available ? "Browse the files your agent can access." : "Start a conversation to browse files.";
      if (available) roots(); else tree.replaceChildren();
    }
  };
})();
