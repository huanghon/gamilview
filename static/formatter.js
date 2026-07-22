(function () {
  const sampleRows = [
    "15663355",
    "15663355 ehdqja9179@gmail.com",
    "70200750 magic22dan@gmail.com",
    "70200038 - noreply@example.com",
    "70200038 gmail1 noreply@example.com",
    "01080792425 sms",
    "01080792425 sms:all",
    "01080792425 sms:default",
  ];

  const SMS_SOURCE_TOKENS = new Set(["sms", "sms_kr", "sms-source", "script"]);

  const INBOX_TOKENS = new Set([
    "gmail1", "gmail2", "gmail3",
    "ehdqja9179@gmail.com",
    "magic22dan@gmail.com",
    "chlqlrkfdl@gmail.com",
    "-", "all", "*",
  ]);

  function isSmsSourceToken(value) {
    if (!value) return false;
    const v = String(value).toLowerCase();
    if (SMS_SOURCE_TOKENS.has(v)) return true;
    return v.startsWith("sms:") || v.startsWith("sms_");
  }

  function parseSmsToken(value) {
    const raw = String(value || "").trim();
    const lower = raw.toLowerCase();
    if (SMS_SOURCE_TOKENS.has(lower)) {
      return { source: "sms", sms_source_ref: "" };
    }
    if (lower.startsWith("sms:")) {
      return { source: "sms", sms_source_ref: raw.slice(4).trim() };
    }
    if (lower.startsWith("sms_")) {
      return { source: "sms", sms_source_ref: raw.slice(4).replace(/^[:_-]+/, "").trim() };
    }
    return null;
  }

  function isInboxToken(value) {
    if (!value) return false;
    return INBOX_TOKENS.has(value.toLowerCase());
  }

  function normalizeInbox(value) {
    if (!value) return "";
    const v = value.toLowerCase();
    if (v === "-" || v === "all" || v === "*") return "";
    return value;
  }

  const sourceInput = document.getElementById("sourceInput");
  const resultOutput = document.getElementById("resultOutput");
  const resultCount = document.getElementById("resultCount");
  const errorPanel = document.getElementById("errorPanel");
  const errorList = document.getElementById("errorList");
  const copyStatus = document.getElementById("copyStatus");
  const generateBtn = document.getElementById("generateBtn");
  const windowMinutesInput = document.getElementById("windowMinutes");
  const windowUnlimitedInput = document.getElementById("windowUnlimited");

  const smsNameInput = document.getElementById("smsNameInput");
  const smsUrlInput = document.getElementById("smsUrlInput");
  const smsNoteInput = document.getElementById("smsNoteInput");
  const smsEnabledInput = document.getElementById("smsEnabledInput");
  const smsDefaultInput = document.getElementById("smsDefaultInput");
  const smsEditId = document.getElementById("smsEditId");
  const smsSaveBtn = document.getElementById("smsSaveBtn");
  const smsResetBtn = document.getElementById("smsResetBtn");
  const smsTestBtn = document.getElementById("smsTestBtn");
  const smsReloadBtn = document.getElementById("smsReloadBtn");
  const smsFormStatus = document.getElementById("smsFormStatus");
  const smsSourceTableBody = document.getElementById("smsSourceTableBody");
  const sourceCount = document.getElementById("sourceCount");

  function readWindowSeconds() {
    if (windowUnlimitedInput && windowUnlimitedInput.checked) return 0;
    const raw = (windowMinutesInput && windowMinutesInput.value) || "";
    const minutes = Number.parseFloat(raw);
    if (!Number.isFinite(minutes) || minutes <= 0) return 0;
    return Math.round(minutes * 60);
  }

  if (windowUnlimitedInput && windowMinutesInput) {
    windowUnlimitedInput.addEventListener("change", () => {
      windowMinutesInput.disabled = windowUnlimitedInput.checked;
    });
  }

  function parseRows(rawText) {
    const rows = rawText.split(/\r?\n/);
    const items = [];
    const errors = [];

    rows.forEach((row, index) => {
      const line = row.trim();
      if (!line) return;

      const parts = line.split(/[\s,]+/).filter(Boolean);
      const phone = parts[0] || "";

      if (!phone) {
        errors.push(`第 ${index + 1} 行缺少手机号：${line}`);
        return;
      }

      let gmailAccount = "";
      let sender = "";
      let source = "gmail";
      let smsSourceRef = "";

      if (parts.length === 2) {
        const second = parts[1];
        const smsToken = parseSmsToken(second);
        if (smsToken) {
          source = "sms";
          smsSourceRef = smsToken.sms_source_ref;
        } else if (isInboxToken(second)) {
          gmailAccount = normalizeInbox(second);
        } else if (second.includes("@")) {
          sender = second;
        } else {
          gmailAccount = second;
        }
      } else if (parts.length === 3) {
        const smsToken = parseSmsToken(parts[1]);
        if (smsToken) {
          source = "sms";
          smsSourceRef = smsToken.sms_source_ref;
        } else {
          gmailAccount = normalizeInbox(parts[1]);
          sender = parts[2];
        }
      } else if (parts.length > 3) {
        errors.push(`第 ${index + 1} 行字段过多（最多 3 列：手机号 收件箱/sms/sms:源名 发件人邮箱）：${line}`);
        return;
      }

      items.push({
        row: index + 1,
        phone,
        source,
        sms_source_ref: source === "sms" ? smsSourceRef : "",
        gmail_account: source === "sms" ? "" : gmailAccount,
        sender: source === "sms" ? "" : sender,
      });
    });

    return { items, errors };
  }

  function renderErrors(errors) {
    errorList.innerHTML = "";
    errorPanel.hidden = errors.length === 0;

    errors.forEach((message) => {
      const item = document.createElement("p");
      item.textContent = message;
      errorList.appendChild(item);
    });
  }

  async function createRecordLink(item) {
    const response = await fetch("/api/record-links", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        phone: item.phone,
        source: item.source || "gmail",
        sms_source_ref: item.sms_source_ref || "",
        gmail_account: item.gmail_account,
        sender: item.sender,
        window_seconds: item.window_seconds,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    return data;
  }

  async function generate() {
    const { items, errors } = parseRows(sourceInput.value);
    const outputs = [];
    const allErrors = [...errors];

    resultOutput.value = "";
    resultCount.textContent = "0 条";
    copyStatus.textContent = "";
    renderErrors(allErrors);

    if (!items.length) {
      return;
    }

    const windowSeconds = readWindowSeconds();

    generateBtn.disabled = true;
    generateBtn.textContent = "生成中";

    for (const item of items) {
      try {
        const data = await createRecordLink({ ...item, window_seconds: windowSeconds });
        outputs.push(`${data.phone}----${data.url}`);
        resultOutput.value = outputs.join("\n");
        resultCount.textContent = `${outputs.length} 条`;
      } catch (error) {
        allErrors.push(`第 ${item.row} 行生成失败：${error.message}`);
        renderErrors(allErrors);
      }
    }

    generateBtn.disabled = false;
    generateBtn.textContent = "生成链接";
  }

  async function copyResult() {
    const text = resultOutput.value.trim();
    copyStatus.textContent = "";

    if (!text) {
      copyStatus.textContent = "没有可复制的链接";
      return;
    }

    try {
      await navigator.clipboard.writeText(text);
      copyStatus.textContent = "已复制";
    } catch (error) {
      resultOutput.focus();
      resultOutput.select();
      const copied = document.execCommand("copy");
      copyStatus.textContent = copied ? "已复制" : "复制失败，请手动复制";
    }
  }

  function setFormStatus(message, isError) {
    if (!smsFormStatus) return;
    smsFormStatus.textContent = message || "";
    smsFormStatus.style.color = isError ? "#b42318" : "";
  }

  function resetSmsForm() {
    if (!smsEditId) return;
    smsEditId.value = "";
    smsNameInput.value = "";
    smsUrlInput.value = "";
    smsNoteInput.value = "";
    smsEnabledInput.checked = true;
    smsDefaultInput.checked = false;
    smsSaveBtn.textContent = "新增源";
    setFormStatus("");
  }

  function fillSmsForm(source) {
    smsEditId.value = String(source.id);
    smsNameInput.value = source.name || "";
    smsUrlInput.value = source.url || "";
    smsNoteInput.value = source.note || "";
    smsEnabledInput.checked = !!source.enabled;
    smsDefaultInput.checked = !!source.is_default;
    smsSaveBtn.textContent = "保存修改";
    setFormStatus(`正在编辑：${source.name}`);
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderSmsSources(sources) {
    if (!smsSourceTableBody) return;
    sourceCount.textContent = `${sources.length} 个`;
    if (!sources.length) {
      smsSourceTableBody.innerHTML = '<tr><td colspan="6" class="empty-row">暂无数据源，可在上方新增，或依赖 .env 中的 SMS_SCRIPT_URL</td></tr>';
      return;
    }

    smsSourceTableBody.innerHTML = sources.map((item) => {
      const enabledTag = item.enabled
        ? '<span class="tag tag-yes">启用</span>'
        : '<span class="tag tag-no">停用</span>';
      const defaultTag = item.is_default
        ? '<span class="tag tag-yes">默认</span>'
        : '<span class="tag tag-no">-</span>';
      return `
        <tr data-id="${item.id}">
          <td><strong>${escapeHtml(item.name)}</strong></td>
          <td class="url-cell">${escapeHtml(item.url)}</td>
          <td>${defaultTag}</td>
          <td>${enabledTag}</td>
          <td>${escapeHtml(item.note || "")}</td>
          <td>
            <div class="row-actions">
              <button type="button" class="secondary-button" data-action="edit">编辑</button>
              <button type="button" class="secondary-button" data-action="test">测试</button>
              <button type="button" class="danger-button" data-action="delete">删除</button>
            </div>
          </td>
        </tr>
      `;
    }).join("");
  }

  async function loadSmsSources() {
    if (!smsSourceTableBody) return;
    try {
      const response = await fetch("/api/sms-sources");
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      renderSmsSources(data.sources || []);
    } catch (error) {
      smsSourceTableBody.innerHTML = `<tr><td colspan="6" class="empty-row">加载失败：${escapeHtml(error.message)}</td></tr>`;
      sourceCount.textContent = "0 个";
    }
  }

  async function saveSmsSource() {
    const payload = {
      name: (smsNameInput.value || "").trim(),
      url: (smsUrlInput.value || "").trim(),
      note: (smsNoteInput.value || "").trim(),
      enabled: !!smsEnabledInput.checked,
      is_default: !!smsDefaultInput.checked,
    };
    if (!payload.name || !payload.url) {
      setFormStatus("名称和 URL 必填", true);
      return;
    }

    const editId = (smsEditId.value || "").trim();
    const method = editId ? "PUT" : "POST";
    const url = editId ? `/api/sms-sources/${editId}` : "/api/sms-sources";

    smsSaveBtn.disabled = true;
    try {
      const response = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      resetSmsForm();
      setFormStatus(editId ? "已保存修改" : "已新增数据源");
      await loadSmsSources();
    } catch (error) {
      setFormStatus(error.message, true);
    } finally {
      smsSaveBtn.disabled = false;
    }
  }

  async function testSmsSource(url, id) {
    setFormStatus("测试中…");
    try {
      const response = await fetch("/api/sms-sources/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url || "", id: id || null }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      const mapped = data.mapped_headers || {};
      setFormStatus(
        `测试成功：${data.record_count || 0} 条；列映射 时间=${mapped.received || "-"} / 手机=${mapped.phone || "-"} / 正文=${mapped.body || "-"}`
      );
    } catch (error) {
      setFormStatus(`测试失败：${error.message}`, true);
    }
  }

  async function deleteSmsSource(id, name) {
    if (!window.confirm(`确认删除 SMS 源「${name}」？`)) return;
    try {
      const response = await fetch(`/api/sms-sources/${id}`, { method: "DELETE" });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      if (String(smsEditId.value) === String(id)) {
        resetSmsForm();
      }
      setFormStatus(`已删除：${name}`);
      await loadSmsSources();
    } catch (error) {
      setFormStatus(error.message, true);
    }
  }

  if (smsSourceTableBody) {
    smsSourceTableBody.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const row = button.closest("tr[data-id]");
      if (!row) return;
      const id = Number(row.getAttribute("data-id"));
      const action = button.getAttribute("data-action");

      const response = await fetch("/api/sms-sources");
      const data = await response.json().catch(() => ({}));
      const source = (data.sources || []).find((item) => Number(item.id) === id);
      if (!source) {
        setFormStatus("数据源不存在或已刷新，请重试", true);
        await loadSmsSources();
        return;
      }

      if (action === "edit") {
        fillSmsForm(source);
      } else if (action === "test") {
        await testSmsSource(source.url, source.id);
      } else if (action === "delete") {
        await deleteSmsSource(source.id, source.name);
      }
    });
  }

  if (smsSaveBtn) smsSaveBtn.addEventListener("click", saveSmsSource);
  if (smsResetBtn) smsResetBtn.addEventListener("click", resetSmsForm);
  if (smsReloadBtn) smsReloadBtn.addEventListener("click", loadSmsSources);
  if (smsTestBtn) {
    smsTestBtn.addEventListener("click", async () => {
      const url = (smsUrlInput.value || "").trim();
      const id = (smsEditId.value || "").trim();
      if (!url && !id) {
        setFormStatus("请先填写 URL 或选择一个已有源", true);
        return;
      }
      await testSmsSource(url, id ? Number(id) : null);
    });
  }

  generateBtn.addEventListener("click", generate);
  document.getElementById("copyBtn").addEventListener("click", copyResult);
  document.getElementById("clearBtn").addEventListener("click", () => {
    sourceInput.value = "";
    resultOutput.value = "";
    resultCount.textContent = "0 条";
    copyStatus.textContent = "";
    renderErrors([]);
    sourceInput.focus();
  });
  document.getElementById("sampleBtn").addEventListener("click", () => {
    sourceInput.value = sampleRows.join("\n");
    resultOutput.value = "";
    resultCount.textContent = "0 条";
    copyStatus.textContent = "";
    renderErrors([]);
  });

  loadSmsSources();
})();
