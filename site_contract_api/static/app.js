"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const html = $("html"), rules = $("rules");
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const encoder = new TextEncoder();
  const BOM_BYTES = 3;
  let busy = false, result = null, revision = 0, limits = null, sizeTimer = 0;
  const bytes = (text) => encoder.encode(text).length;
  const size = (value) => `${value.toLocaleString()} bytes`;
  function base64(data) {
    if (typeof data.toBase64 === "function") return data.toBase64();
    // Chunks are a multiple of three bytes, so the encoded pieces join without padding.
    const parts = [];
    for (let start = 0; start < data.length; start += 24576) {
      parts.push(btoa(String.fromCharCode.apply(null, data.subarray(start, start + 24576))));
    }
    return parts.join("");
  }
  function status(title, message, kind = "") {
    $("state").textContent = title; $("status").textContent = message;
    document.body.dataset.state = kind;
  }
  function controls() {
    for (const id of ["demo", "break", "preview", "read-api", "html-file", "rules-file"]) $(id).disabled = busy || !limits;
    html.readOnly = rules.readOnly = busy;
    $("copy").disabled = $("download").disabled = busy || !result;
    $("workspace").setAttribute("aria-busy", String(busy));
  }
  function showSize() {
    sizeTimer = 0;
    const count = bytes(html.value);
    $("html-size").textContent = limits && count > limits.html_bytes ? `${size(count)} (over the limit)` : size(count);
  }
  function changed() {
    revision += 1; result = null; controls();
    if (!sizeTimer) sizeTimer = setTimeout(showSize, 150);
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
  function request(configured) {
    if (configured) return {};
    if (/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?:^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(html.value)) throw new Error("HTML contains an unpaired Unicode surrogate. Save or export the page as valid UTF-8.");
    const page = encoder.encode(html.value), rulesSize = bytes(rules.value);
    if (page.length > limits.html_bytes) throw new Error(`HTML is ${size(page.length)} as UTF-8; the limit is ${size(limits.html_bytes)}.`);
    if (rulesSize > limits.rules_bytes) throw new Error(`Rules are ${size(rulesSize)}; the limit is ${size(limits.rules_bytes)}.`);
    const body = JSON.stringify({html_b64: base64(page), rules: rules.value});
    const sent = bytes(body);
    if (sent > limits.request_bytes) throw new Error(`The request is ${size(sent)}; the limit is ${size(limits.request_bytes)}. Shorten the rules.`);
    return {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body};
  }
  async function preview(configured = false) {
    if (busy || !limits) return;
    const current = ++revision, snapshot = [html.value, rules.value];
    let options;
    try { options = request(configured); }
    catch (error) { status("Input rejected.", error.message, "error"); return; }
    busy = true; result = null; controls(); status("Checking the contract...", "The preview is processed by this local server.");
    try {
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
  for (const [id, target, key, label] of [["html-file", html, "html_bytes", "HTML"], ["rules-file", rules, "rules_bytes", "Rules"]]) {
    $(id).addEventListener("change", async () => {
      if (busy || !limits || !$(id).files.length) return;
      const file = $(id).files[0], limit = limits[key];
      // A leading byte-order mark is removed on import and does not count.
      if (file.size > limit + BOM_BYTES) {
        status("File too large.", `${label} file is ${size(file.size)}; the limit is ${size(limit)}.`, "error"); $(id).value = ""; return;
      }
      busy = true; controls();
      try {
        const text = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer()), count = bytes(text);
        if (count > limit) status("File too large.", `${label} file is ${size(count)} as UTF-8; the limit is ${size(limit)}.`, "error");
        else { target.value = text; changed(); status("File imported.", `${label}: ${size(count)}. Review the input, then run the contract.`); }
      }
      catch (_) { status("Import failed.", "The file must be valid UTF-8 text.", "error"); }
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
  controls();
  fetch("/api/status").then((response) => {
    if (!response.ok) throw new Error();
    return response.json();
  }).then((value) => {
    const published = value.limits || {};
    if (!["html_bytes", "rules_bytes", "request_bytes"].every((key) => Number.isSafeInteger(published[key]) && published[key] > 0)) throw new Error();
    limits = published;
    $("mode").textContent = value.mode;
    $("html-limit").textContent = `${size(limits.html_bytes)} maximum`;
    $("rules-limit").textContent = `16 fields / ${size(limits.rules_bytes)} maximum`;
    controls(); demo();
  }).catch(() => status("Limits unavailable.", "Restart the local server and reload this page.", "error"));
})();
