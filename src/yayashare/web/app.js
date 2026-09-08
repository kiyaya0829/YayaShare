"use strict";
const el = id => document.getElementById(id);
let connected = false, busy = false, activeUpload = null, cancelled = false, signature = "";
function status(message) { el("status").textContent = message; }
function controls() {
  el("send-text").disabled = !connected || busy;
  el("send-files").disabled = !connected || busy || !el("files").files.length;
  el("refresh").disabled = !connected || busy;
  el("files").disabled = busy;
}
async function api(path, body) {
  const options = {credentials: "same-origin", cache: "no-store", headers: {"X-YayaShare": "1"}};
  if (body !== undefined) {
    options.method = "POST"; options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) {
    if ([401, 403].includes(response.status)) { connected = false; controls(); }
    throw new Error(value.error || "操作未完成");
  }
  return value;
}
async function refresh() {
  const value = await api("/api/items");
  connected = true; controls();
  el("connection").textContent = `${value.name} · 剩余 ${Math.ceil(value.remaining / 60)} 分钟`;
  const next = JSON.stringify(value.items);
  if (next === signature) return;
  signature = next;
  el("items").replaceChildren();
  el("empty").hidden = value.items.length > 0;
  for (const item of value.items) {
    const card = document.createElement("article");
    if (item.kind === "text") {
      const text = document.createElement("textarea"); text.readOnly = true;
      text.setAttribute("aria-label", "电脑分享的文字"); text.value = item.text;
      const copy = document.createElement("button"); copy.textContent = "复制文字";
      copy.addEventListener("click", async () => {
        try {
          if (window.isSecureContext && navigator.clipboard) await navigator.clipboard.writeText(item.text);
          else { text.focus(); text.select(); text.setSelectionRange(0, text.value.length); if (!document.execCommand("copy")) throw new Error(); }
          status("已复制文字。");
        } catch (_) { text.focus(); text.select(); text.setSelectionRange(0, text.value.length); status("文字已选中，请长按并选择「复制」。"); }
      });
      card.append(text, copy);
    } else {
      const label = document.createElement("p"); label.textContent = `${item.name} · ${(item.size / 1048576).toFixed(1)} MiB`;
      const link = document.createElement("a"); link.className = "download";
      link.href = `/download/${encodeURIComponent(item.id)}`; link.download = item.name; link.textContent = "下载文件";
      card.append(label, link);
    }
    el("items").append(card);
  }
}
el("send-text").addEventListener("click", async () => {
  const text = el("text").value;
  if (!text || new TextEncoder().encode(text).length > 65536) return status("请输入不超过 64 KiB 的文字。");
  busy = true; controls(); status("正在发送文字…");
  try { await api("/api/text", {text}); status("电脑已确认接收文字。"); }
  catch (error) { status(error.message || "连接中断，请检查电脑入口。"); }
  finally { busy = false; controls(); }
});
el("files").addEventListener("change", () => {
  el("selection").textContent = el("files").files.length ? Array.from(el("files").files).map(f => f.name).join("、") : "尚未选择文件。";
  controls();
});
function upload(file, index, total) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); activeUpload = xhr;
    xhr.open("POST", "/api/file"); xhr.timeout = 20 * 60 * 1000;
    xhr.setRequestHeader("X-YayaShare", "1"); xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = event => {
      el("progress").value = event.lengthComputable ? event.loaded * 100 / event.total : 0;
      status(`正在发送 ${index}/${total}：${file.name}（等待电脑确认后才算完成）`);
    };
    xhr.onload = () => {
      if (xhr.status === 200) return resolve();
      if ([401, 403].includes(xhr.status)) connected = false;
      let message = "发送失败，请检查电脑入口。";
      try { message = JSON.parse(xhr.responseText).error || message; } catch (_) {}
      reject(new Error(message));
    };
    xhr.onerror = () => reject(new Error("连接中断，未确认的文件请在电脑核对后重试。"));
    xhr.ontimeout = () => reject(new Error("发送超时，请检查网络后重试。"));
    xhr.onabort = () => reject(new Error("已取消；已经完成的文件会保留，请在电脑核对。"));
    xhr.send(file);
  });
}
el("send-files").addEventListener("click", async () => {
  const files = Array.from(el("files").files);
  if (!files.length) return;
  if (files.some(f => f.size > 256 * 1048576)) return status("其中有文件超过 256 MiB，请分开处理。");
  busy = true; cancelled = false; controls(); el("progress").hidden = false; el("cancel").hidden = false;
  let done = 0;
  try {
    for (const file of files) {
      if (cancelled) break;
      await upload(file, done + 1, files.length); done++;
    }
    status(`电脑已确认接收 ${done} 个文件。`); el("files").value = ""; el("selection").textContent = "可以继续选择文件。";
  } catch (error) { status(`已完成 ${done} 个。${error.message}`); }
  finally { activeUpload = null; busy = false; controls(); el("progress").hidden = true; el("cancel").hidden = true; }
});
el("cancel").addEventListener("click", () => { cancelled = true; if (activeUpload) activeUpload.abort(); });
el("refresh").addEventListener("click", () => refresh().then(() => status("已刷新电脑分享。")).catch(error => status(error.message)));
(async () => {
  const token = location.hash.slice(1);
  history.replaceState(null, "", "/");
  try {
    if (token) await api("/api/connect", {token});
    await refresh(); status("已连接，可以发送文字和文件。");
  } catch (error) { status(error.message || "无法连接，请在电脑重新开启入口并扫码。"); el("connection").textContent = "尚未连接"; }
  setInterval(() => { if (connected && !busy && !document.hidden) refresh().catch(error => status(error.message || "暂时无法连接电脑，请检查网络。")); }, 5000);
})();
