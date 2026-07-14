(function () {
  const sampleRows = [
    "15663355",
    "15663355 ehdqja9179@gmail.com",
    "70200750 magic22dan@gmail.com",
    "70200038 - noreply@example.com",
    "70200038 gmail1 noreply@example.com",
    "01080792425 sms",
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
    return SMS_SOURCE_TOKENS.has(String(value).toLowerCase());
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

      if (parts.length === 2) {
        // 兼容：第二列可能是 sms / 收件箱别名 / 发件人邮箱
        const second = parts[1];
        if (isSmsSourceToken(second)) {
          source = "sms";
        } else if (isInboxToken(second)) {
          gmailAccount = normalizeInbox(second);
        } else if (second.includes("@")) {
          // 不是已知收件箱别名，但带 @ → 视为发件人邮箱
          sender = second;
        } else {
          gmailAccount = second;
        }
      } else if (parts.length === 3) {
        if (isSmsSourceToken(parts[1])) {
          // 短信源不需要收件箱/发件人，第三列忽略
          source = "sms";
        } else {
          gmailAccount = normalizeInbox(parts[1]);
          sender = parts[2];
        }
      } else if (parts.length > 3) {
        errors.push(`第 ${index + 1} 行字段过多（最多 3 列：手机号 收件箱/sms 发件人邮箱）：${line}`);
        return;
      }

      items.push({
        row: index + 1,
        phone,
        source,
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
})();
