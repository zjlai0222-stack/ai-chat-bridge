const dot = document.getElementById("dot");
const stateEl = document.getElementById("state");
const urlEl = document.getElementById("url");

async function send(msg) {
  return await chrome.runtime.sendMessage(msg);
}

async function refresh() {
  try {
    const res = await send({ type: "popup:getState" });
    if (!res) return;
    stateEl.textContent = res.wsState === "connected" ? "已連線" : res.wsState === "connecting" ? "連線中…" : "未連線";
    dot.className = "dot" + (res.wsState === "connected" ? " on" : res.wsState === "connecting" ? " wait" : "");
    if (document.activeElement !== urlEl) urlEl.value = res.serverUrl;
  } catch (_) {}
}

document.getElementById("saveUrl").addEventListener("click", async () => {
  await send({ type: "popup:setServerUrl", url: urlEl.value.trim() });
  await send({ type: "popup:connect" });
  refresh();
});
document.getElementById("connect").addEventListener("click", async () => { await send({ type: "popup:connect" }); refresh(); });
document.getElementById("disconnect").addEventListener("click", async () => { await send({ type: "popup:disconnect" }); refresh(); });
document.querySelectorAll(".sites button").forEach((btn) => {
  btn.addEventListener("click", () => send({ type: "popup:openSite", site: btn.dataset.site }));
});

refresh();
setInterval(refresh, 2000);
