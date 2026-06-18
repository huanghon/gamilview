(function () {
  function getShell() {
    return document.querySelector(".shell");
  }

  function currentToken() {
    const input = document.getElementById("tokenInput");
    const shell = getShell();
    return (input && input.value) || (shell && shell.dataset.token) || "";
  }

  function api(path, options) {
    const separator = path.includes("?") ? "&" : "?";
    return fetch(`${path}${separator}token=${encodeURIComponent(currentToken())}`, options).then(
      async (response) => {
        const contentType = response.headers.get("content-type") || "";
        const body = contentType.includes("application/json")
          ? await response.json()
          : await response.text();
        if (!response.ok) {
          const detail = body && body.detail ? body.detail : body;
          throw new Error(detail || `HTTP ${response.status}`);
        }
        return body;
      }
    );
  }

  function saveToken() {
    const token = currentToken();
    const url = new URL(window.location.href);
    url.searchParams.set("token", token);
    window.history.replaceState({}, "", url.toString());
  }

  function wireTokenButton() {
    const btn = document.getElementById("saveTokenBtn");
    if (btn) {
      btn.addEventListener("click", saveToken);
    }
  }

  function setStatus(text) {
    const node = document.getElementById("statusText");
    if (node) {
      node.textContent = text || "";
    }
  }

  function formatTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString();
  }

  function initIndex() {
    wireTokenButton();
    const list = document.getElementById("phoneList");
    const empty = document.getElementById("emptyState");
    const refreshAllBtn = document.getElementById("refreshAllBtn");

    async function loadPhones() {
      try {
        const data = await api("/api/phones");
        list.innerHTML = "";
        empty.style.display = data.phones.length ? "none" : "block";
        data.phones.forEach((item) => {
          const link = document.createElement("a");
          link.className = "phone-card";
          link.href = `/mail/${encodeURIComponent(item.phone)}?token=${encodeURIComponent(currentToken())}`;
          link.innerHTML = `<strong>${escapeHtml(item.phone)}</strong><span>${escapeHtml(
            item.gmail_accounts.join(", ")
          )}</span>`;
          list.appendChild(link);
        });
      } catch (error) {
        list.innerHTML = "";
        empty.style.display = "block";
        empty.textContent = error.message;
      }
    }

    if (refreshAllBtn) {
      refreshAllBtn.addEventListener("click", async () => {
        refreshAllBtn.disabled = true;
        refreshAllBtn.textContent = "刷新中";
        try {
          await api("/api/refresh", { method: "POST" });
          await loadPhones();
        } catch (error) {
          empty.style.display = "block";
          empty.textContent = error.message;
        } finally {
          refreshAllBtn.disabled = false;
          refreshAllBtn.textContent = "刷新全部";
        }
      });
    }

    loadPhones();
  }

  function renderMailDetail(mail) {
    const detail = document.getElementById("mailDetail");
    if (!mail) {
      detail.className = "mail-detail empty";
      detail.textContent = "暂无邮件。";
      return;
    }

    detail.className = "mail-detail";
    const body = mail.body_text || mail.snippet || "";
    detail.innerHTML = `
      <div>
        <h2 class="detail-title">${escapeHtml(mail.subject || "(无标题)")}</h2>
      </div>
      <div class="detail-meta">
        <span>发件人：${escapeHtml(mail.sender || "")}</span>
        <span>时间：${escapeHtml(formatTime(mail.received_at))}</span>
        <span>邮箱来源：${escapeHtml(mail.gmail_account || "")}</span>
        <span>Message ID：${escapeHtml(mail.message_id || "")}</span>
      </div>
      <div class="mail-body">${escapeHtml(body)}</div>
    `;

    if (mail.body_html) {
      const frame = document.createElement("iframe");
      frame.className = "html-frame";
      frame.setAttribute("sandbox", "");
      detail.appendChild(frame);
      frame.srcdoc = mail.body_html;
    }
  }

  function initMail() {
    wireTokenButton();
    const shell = getShell();
    const phone = shell.dataset.phone;
    const list = document.getElementById("mailList");
    const count = document.getElementById("mailCount");
    const refreshBtn = document.getElementById("refreshBtn");
    const reloadBtn = document.getElementById("reloadBtn");

    async function loadMails(selectFirst) {
      setStatus("加载中...");
      try {
        const data = await api(`/api/mails/${encodeURIComponent(phone)}`);
        list.innerHTML = "";
        count.textContent = String(data.mails.length);
        data.mails.forEach((mail, index) => {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "mail-item";
          btn.dataset.messageId = mail.message_id;
          btn.innerHTML = `
            <span class="mail-subject">${escapeHtml(mail.subject || "(无标题)")}</span>
            <span class="meta">${escapeHtml(mail.sender || "")}</span>
            <span class="meta">${escapeHtml(formatTime(mail.received_at))}</span>
            <span class="meta">${escapeHtml(mail.gmail_account || "")}</span>
          `;
          btn.addEventListener("click", () => loadDetail(mail.message_id, btn));
          list.appendChild(btn);
          if (selectFirst && index === 0) {
            loadDetail(mail.message_id, btn);
          }
        });
        if (!data.mails.length) {
          renderMailDetail(null);
        }
        setStatus("已加载");
      } catch (error) {
        setStatus(error.message);
      }
    }

    async function loadDetail(messageId, button) {
      document.querySelectorAll(".mail-item.active").forEach((node) => {
        node.classList.remove("active");
      });
      if (button) {
        button.classList.add("active");
      }
      setStatus("读取邮件详情...");
      try {
        const mail = await api(
          `/api/mail-detail/${encodeURIComponent(messageId)}?phone=${encodeURIComponent(phone)}`
        );
        renderMailDetail(mail);
        setStatus("已加载");
      } catch (error) {
        setStatus(error.message);
      }
    }

    if (refreshBtn) {
      refreshBtn.addEventListener("click", async () => {
        refreshBtn.disabled = true;
        refreshBtn.textContent = "刷新中";
        setStatus("正在读取 Gmail...");
        try {
          await api(`/api/refresh?phone=${encodeURIComponent(phone)}`, { method: "POST" });
          await loadMails(true);
        } catch (error) {
          setStatus(error.message);
        } finally {
          refreshBtn.disabled = false;
          refreshBtn.textContent = "手动刷新";
        }
      });
    }

    if (reloadBtn) {
      reloadBtn.addEventListener("click", () => loadMails(true));
    }

    loadMails(true);
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  window.MailApp = { initIndex, initMail };
})();
