"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const html = $("html"), rules = $("rules");
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  let busy = false, result = null, revision = 0;
  const bytes = (text) => new TextEncoder().encode(text).length;
  function status(title, message, kind = "") {
    $("state").textContent = title; $("status").textContent = message;
    document.body.dataset.state = kind;
  }
  function controls() {
    for (const id of ["demo", "break", "preview", "read-api", "html-file", "rules-file"]) $(id).disabled = busy;
    html.readOnly = rules.readOnly = busy;
    $("copy").disabled = $("download").disabled = busy || !result;
    $("workspace").setAttribute("aria-busy", String(busy));
  }
  function changed() {
    revision += 1; result = null; controls();
    $("html-size").textContent = `${bytes(html.value).toLocaleString()} bytes`;
    $("result-badge").textContent = "Changed / run again";
  }
  function render(value) {
    result = value;
    $("output").textContent = JSON.stringify(value, null, 2);
    $("result-badge").textContent = value.ok ? "Contract passed" : "Contract failed";
    $("result-title").textContent = value.ok ? "A result you can trace." : "The page broke its contract.";
    $("count").textContent = value.ok ? String(value.items.length) : "-";
    for (const [id, key] of [["source-hash", "source_hash"], ["rules-hash", "rules_hash"]]) {
      const hash = value.provenance?.[key] || "Not produced";
      $(id).textContent = hash.length === 64 ? `${hash.slice(0, 18)}...` : hash;
      $(id).title = hash;
    }
    status(value.ok ? "Contract passed." : "Contract failed.", value.ok
      ? `${value.items.length} items extracted with explicit types and selectors.`
      : value.message || "The input could not be processed.", value.ok ? "success" : "error");
  }
  async function preview(configured = false) {
    if (busy) return;
    const current = ++revision, snapshot = [html.value, rules.value];
    if (!configured && (bytes(html.value) > 1048576 || bytes(rules.value) > 32768)) {
      status("Input too large.", "Use up to 1 MB of HTML and 32 KB of rules.", "error"); return;
    }
    busy = true; result = null; controls(); status("Checking the contract...", "The preview is processed by this local server.");
    try {
      const options = configured ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify({html: html.value, rules: rules.value})};
      const response = await fetch(configured ? "/api/data" : "/api/preview", options);
      const value = await response.json();
      if (current !== revision || (!configured && (snapshot[0] !== html.value || snapshot[1] !== rules.value))) {
        throw new Error("The input changed during processing. Run the contract again.");
      }
      render(value);
      $("output-label").textContent = configured ? "Configured /api/data response" : "Editor preview response";
    } catch (error) {
      result = null; status("Preview unavailable.", error.message, "error");
    } finally { busy = false; controls(); }
  }
  async function demo() {
    if (busy) return;
    busy = true; controls();
    try {
      const response = await fetch("/api/demo"); const value = await response.json();
      html.value = value.html; rules.value = JSON.stringify(value.rules, null, 2); changed();
      status("Sample loaded.", "A fictional workshop catalog, with three products and five fields.");
    } catch (_) { status("Sample unavailable.", "Restart the local server and try again.", "error"); }
    finally { busy = false; controls(); }
  }
  for (const element of [html, rules]) element.addEventListener("input", changed);
  for (const [id, target, limit] of [["html-file", html, 1048576], ["rules-file", rules, 32768]]) {
    $(id).addEventListener("change", async () => {
      if (busy || !$(id).files.length) return;
      const file = $(id).files[0];
      if (file.size > limit) { status("File too large.", "Use a smaller input file.", "error"); $(id).value = ""; return; }
      busy = true; controls();
      try { target.value = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer()); changed(); status("File imported.", "Review the input, then run the contract."); }
      catch (_) { status("Import failed.", "The file must be readable UTF-8 text.", "error"); }
      finally { busy = false; $(id).value = ""; controls(); }
    });
  }
  $("demo").addEventListener("click", demo);
  $("preview").addEventListener("click", () => preview());
  $("read-api").addEventListener("click", () => preview(true));
  $("break").addEventListener("click", () => {
    if (busy) return;
    html.value = html.value.replace('data-price="24.50"', 'data-cost="24.50"'); changed();
    status("Sample layout changed.", "The first price attribute moved. Run the contract to see the failure.");
  });
  $("copy").addEventListener("click", async () => {
    if (busy || !result) return;
    try { await navigator.clipboard.writeText(JSON.stringify(result, null, 2)); status("JSON copied.", "Only the displayed response was copied."); }
    catch (_) { status("Copy unavailable.", "Select the JSON output or use Download JSON.", "error"); }
  });
  $("download").addEventListener("click", () => {
    if (busy || !result) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], {type: "application/json"}));
    const link = document.createElement("a"); link.href = url; link.download = "contract-result.json";
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  fetch("/api/status").then((response) => response.json()).then((value) => { $("mode").textContent = value.mode; }).catch(() => {});
  demo();
})();
