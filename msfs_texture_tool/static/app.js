const state = {
  files: [],
  presets: [],
};

const els = {
  status: document.querySelector("#toolStatus"),
  dropzone: document.querySelector("#dropzone"),
  fileInput: document.querySelector("#fileInput"),
  fileList: document.querySelector("#fileList"),
  resultList: document.querySelector("#resultList"),
  convertBtn: document.querySelector("#convertBtn"),
  clearBtn: document.querySelector("#clearBtn"),
  backend: document.querySelector("#backend"),
  quality: document.querySelector("#quality"),
  transfer: document.querySelector("#transfer"),
  mipmaps: document.querySelector("#mipmaps"),
  metadata: document.querySelector("#metadata"),
  flagsFile: document.querySelector("#flagsFile"),
};

function detectPresetName(fileName) {
  const upper = fileName.replace(/\.png$/i, "").toUpperCase();
  for (const preset of state.presets) {
    if (preset.key === "generic") continue;
    if (preset.suffixes.some((suffix) => upper.endsWith(suffix))) {
      return preset;
    }
  }
  return state.presets.find((preset) => preset.key === "generic") || {
    key: "generic",
    label: "Generic Texture",
    transfer: "srgb",
  };
}

function formatSize(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function setFiles(files) {
  const pngs = Array.from(files).filter((file) => file.name.toLowerCase().endsWith(".png"));
  const byName = new Map(state.files.map((file) => [file.name, file]));
  for (const file of pngs) byName.set(file.name, file);
  state.files = Array.from(byName.values());
  renderFiles();
}

function renderFiles() {
  els.convertBtn.disabled = state.files.length === 0;
  if (!state.files.length) {
    els.fileList.className = "fileList empty";
    els.fileList.textContent = "还没有文件";
    return;
  }

  els.fileList.className = "fileList";
  els.fileList.innerHTML = state.files
    .map((file) => {
      const preset = detectPresetName(file.name);
      const transfer = els.transfer.value === "auto" ? preset.transfer : els.transfer.value;
      return `
        <div class="row">
          <div>
            <div class="name">${escapeHtml(file.name)}</div>
            <div class="meta">${formatSize(file.size)}</div>
          </div>
          <span class="pill">${escapeHtml(preset.label)}</span>
          <span class="pill">${transfer.toUpperCase()}</span>
          <div class="meta">输出：${escapeHtml(file.name)}.KTX2 / .json / .FLAGS</div>
        </div>
      `;
    })
    .join("");
}

function renderResults(results) {
  if (!results.length) {
    els.resultList.className = "resultList empty";
    els.resultList.textContent = "转换结果会显示在这里";
    return;
  }

  els.resultList.className = "resultList";
  els.resultList.innerHTML = results
    .map((result) => {
      if (result.status === "error") {
        return `
          <div class="row resultRow">
            <div class="name">${escapeHtml(result.sourceName)}</div>
            <div class="error">失败</div>
            <div class="log">${escapeHtml(result.error)}</div>
          </div>
        `;
      }

      const links = Object.entries(result.outputs)
        .map(([key, href]) => `<a href="${href}" download>${key}</a>`)
        .join("");
      const command = result.command ? result.command.join(" ") : "";
      return `
        <div class="row resultRow">
          <div>
            <div class="name">${escapeHtml(result.sourceName)}</div>
            <div class="meta">${escapeHtml(result.presetLabel)} · ${escapeHtml(result.transfer.toUpperCase())} · ${escapeHtml(result.quality)}</div>
          </div>
          <div class="ok">完成</div>
          <div>
            <div class="links">${links}</div>
            <div class="log">${escapeHtml(command)}</div>
          </div>
        </div>
      `;
    })
    .join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function loadPresets() {
  const response = await fetch("/api/presets");
  const data = await response.json();
  state.presets = data.presets || [];
  const basisuText = data.basisu ? `basisu: ${data.basisu}` : "未找到 basisu";
  const exactText = data.sdkExactAvailable ? "SDK Exact 可用" : "SDK Exact 未配置";
  els.status.textContent = `${basisuText} · ${exactText}`;
  renderFiles();
}

async function convert() {
  if (!state.files.length) return;
  els.convertBtn.disabled = true;
  els.convertBtn.textContent = "转换中";
  els.resultList.className = "resultList empty";
  els.resultList.textContent = "正在转换...";

  const form = new FormData();
  for (const file of state.files) form.append("files", file, file.name);
  form.append("backend", els.backend.value);
  form.append("quality", els.quality.value);
  form.append("transfer", els.transfer.value);
  form.append("mipmaps", String(els.mipmaps.checked));
  form.append("metadata", String(els.metadata.checked));
  form.append("flagsFile", String(els.flagsFile.checked));

  try {
    const response = await fetch("/api/convert", { method: "POST", body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "转换失败");
    renderResults(data.results || []);
  } catch (error) {
    renderResults([{ sourceName: "批量任务", status: "error", error: error.message }]);
  } finally {
    els.convertBtn.disabled = state.files.length === 0;
    els.convertBtn.textContent = "转换";
  }
}

els.fileInput.addEventListener("change", (event) => setFiles(event.target.files));
els.convertBtn.addEventListener("click", convert);
els.clearBtn.addEventListener("click", () => {
  state.files = [];
  els.fileInput.value = "";
  renderFiles();
  renderResults([]);
});

for (const id of ["quality", "transfer", "mipmaps", "metadata", "flagsFile"]) {
  els[id].addEventListener("change", renderFiles);
}

for (const eventName of ["dragenter", "dragover"]) {
  els.dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    els.dropzone.classList.add("active");
  });
}

for (const eventName of ["dragleave", "drop"]) {
  els.dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    els.dropzone.classList.remove("active");
  });
}

els.dropzone.addEventListener("drop", (event) => {
  setFiles(event.dataTransfer.files);
});

loadPresets().catch((error) => {
  els.status.textContent = `初始化失败：${error.message}`;
});
