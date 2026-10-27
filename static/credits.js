/* =========================================================================
   /app/credits + /app/credits/orders/<id> —— 充值 Credit 页（D1）
   独立轻量脚本（不加载 Viewer/app shell，无外部依赖）。

   约束：
   - 金额全程 BigInt；Credit 显示 = units / 10^7 截断到 4 位小数，非零但
     < 0.0001 显示「小于 0.0001」；
   - 建单幂等键每次「尝试」生成一次（crypto.getRandomValues），双击/重试复用；
   - 只有 https 且 host 为 alipay.com / alipaydev.com（含子域）才跳转 pay_url；
   - 订单页轮询 2s→10s 退避，终态或约 10 分钟后停止，回到前台立即恢复；
   - 数据加载失败显示「不可用」，绝不显示 0。
   ========================================================================= */
'use strict';

(function () {
  var UNITS_PER_CREDIT = 10000000n;
  var UNITS_PER_FEN = 10000000n;
  var NANO_PER_CENT = 10000000n;
  var POLL_START_MS = 2000;
  var POLL_MAX_MS = 10000;
  var POLL_LIMIT_MS = 10 * 60 * 1000;

  function t(key, vars) {
    if (window.HP_I18N && window.HP_I18N.t) return window.HP_I18N.t(key, vars);
    return key;
  }

  function $(id) {
    return document.getElementById(id);
  }

  function setText(id, value) {
    var el = $(id);
    if (el) el.textContent = value;
  }

  function setHidden(id, hidden) {
    var el = $(id);
    if (el) el.hidden = !!hidden;
  }

  function toggleClass(el, name, on) {
    if (!el || !el.classList) return;
    if (on) el.classList.add(name);
    else el.classList.remove(name);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /* ------------------------------ 数值格式化 ---------------------------- */
  function groupThousands(digits) {
    var out = "";
    var count = 0;
    for (var i = digits.length - 1; i >= 0; i--) {
      out = digits.charAt(i) + out;
      count += 1;
      if (count % 3 === 0 && i > 0) out = "," + out;
    }
    return out;
  }

  /** credit_units → Credit 文本（截断 4 位小数；极小非零单独提示）。 */
  function formatCreditsUnits(units) {
    if (units === null || units === undefined || units === "") return null;
    var b;
    try { b = BigInt(String(units)); } catch (e) { return null; }
    var negative = b < 0n;
    if (negative) b = -b;
    var whole = b / UNITS_PER_CREDIT;
    // 4 位小数 = floor(units % 1e7 / 1e3)，纯整数运算，不经过 Number
    var frac = (b % UNITS_PER_CREDIT) / 1000n;
    if (whole === 0n && frac === 0n) {
      return b !== 0n ? t("credit.balance.less_than_min") : "0";
    }
    var fracText = frac.toString().padStart(4, "0").replace(/0+$/, "");
    var text = groupThousands(whole.toString()) +
      (fracText ? "." + fracText : "");
    return (negative ? "-" : "") + text;
  }

  /** fen → ¥ 文本（两位小数，整数运算）。 */
  function formatCnyFen(fen) {
    if (fen === null || fen === undefined || fen === "") return null;
    var b;
    try { b = BigInt(String(fen)); } catch (e) { return null; }
    var negative = b < 0n;
    if (negative) b = -b;
    var whole = b / 100n;
    var frac = (b % 100n).toString().padStart(2, "0");
    var sign = negative && (whole !== 0n || frac !== "00") ? "-" : "";
    return "¥" + sign + groupThousands(whole.toString()) + "." + frac;
  }

  /** nano-CNY → CNY 文本（两位小数，半分进位 away from zero）。 */
  function formatCnyNano(nano) {
    if (nano === null || nano === undefined || nano === "") return null;
    var b;
    try { b = BigInt(String(nano)); } catch (e) { return null; }
    var negative = b < 0n;
    if (negative) b = -b;
    var cents = (b + NANO_PER_CENT / 2n) / NANO_PER_CENT;
    var whole = cents / 100n;
    var frac = (cents % 100n).toString().padStart(2, "0");
    var sign = negative && cents !== 0n ? "-" : "";
    return "¥" + sign + groupThousands(whole.toString()) + "." + frac;
  }

  /** 带符号的 ledger amount_units。 */
  function formatSignedUnits(units) {
    if (units === null || units === undefined || units === "") return null;
    var b;
    try { b = BigInt(String(units)); } catch (e) { return null; }
    if (b === 0n) return "0";
    var text = formatCreditsUnits((b < 0n ? -b : b).toString());
    if (text === null) return null;
    return (b < 0n ? "-" : "+") + text;
  }

  function formatTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + "." + pad(d.getMonth() + 1) + "." +
      pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  /* ------------------------------ 支付 URL 校验 ------------------------- */
  function hostAllowed(host) {
    function match(base) {
      return host === base || host.slice(-(base.length + 1)) === "." + base;
    }
    return match("alipay.com") || match("alipaydev.com");
  }

  function isAllowedPayUrl(url) {
    if (typeof url !== "string" || !url) return false;
    var parsed;
    try { parsed = new URL(url); } catch (e) { return false; }
    if (parsed.protocol !== "https:") return false;
    return hostAllowed(String(parsed.hostname || "").toLowerCase());
  }

  /* ------------------------------ 幂等键 -------------------------------- */
  function generateKey(cryptoObj) {
    var bytes = new Uint8Array(24);
    var c = cryptoObj || window.crypto || window.msCrypto;
    if (c && typeof c.getRandomValues === "function") {
      c.getRandomValues(bytes);
    } else {
      for (var i = 0; i < bytes.length; i++) {
        bytes[i] = Math.floor(Math.random() * 256);
      }
    }
    var hex = "";
    for (var j = 0; j < bytes.length; j++) {
      hex += (bytes[j] < 16 ? "0" : "") + bytes[j].toString(16);
    }
    // 50 字符，满足 [A-Za-z0-9_-]{8,128}
    return "hp" + hex;
  }

  function AttemptKeys(cryptoObj) {
    this._crypto = cryptoObj || null;
    this._key = null;
    this._signature = null;
  }
  AttemptKeys.prototype.forAttempt = function (signature) {
    if (!this._key || this._signature !== signature) {
      this._key = generateKey(this._crypto);
      this._signature = signature;
    }
    return this._key;
  };
  AttemptKeys.prototype.current = function () { return this._key; };
  AttemptKeys.prototype.reset = function () {
    this._key = null;
    this._signature = null;
  };

  /* ------------------------------ 状态文案 ------------------------------ */
  function orderStateKey(order) {
    if (!order || typeof order !== "object") return "credit.state.unavailable";
    if (order.credit_state === "credited") return "credit.state.credited";
    if (order.payment_state === "closed") return "credit.state.closed";
    if (order.payment_state === "paid") return "credit.state.processing";
    if (order.payment_state === "created" || order.payment_state === "paying") {
      return "credit.state.pending";
    }
    return "credit.state.unavailable";
  }

  function isTerminalOrder(order) {
    return !!order && (order.credit_state === "credited" ||
      order.payment_state === "closed");
  }

  function topupBlockedKey(reason) {
    switch (reason) {
      case "preview": return "credit.state.preview";
      case "disabled": return "credit.state.disabled";
      case "paused": return "credit.state.paused";
      case "config_unavailable": return "credit.state.config_unavailable";
      case "wallet_not_credits": return "credit.state.wallet_not_credits";
      case "wallet_inactive": return "credit.state.wallet_inactive";
      case "user_disabled": return "credit.state.user_disabled";
      case "not_user": return "credit.state.not_user";
      default: return "credit.state.generic_blocked";
    }
  }

  function isNonZeroUnits(value) {
    if (value === null || value === undefined || value === "") return false;
    try { return BigInt(String(value)) !== 0n; } catch (e) { return false; }
  }

  /* ------------------------------ 消息区 -------------------------------- */
  function setMessage(id, text, kind) {
    var box = $(id);
    if (!box) return;
    box.textContent = text || "";
    box.hidden = !text;
    toggleClass(box, "credit-error", kind === "error");
    toggleClass(box, "credit-success", kind === "success");
  }

  /* ------------------------------ 账户/余额 ----------------------------- */
  function renderAccountUnavailable(state, status, code) {
    state.account = null;
    state.accountError = { status: status, code: code };
    state.balanceUnits = null;
    state.canTopup = false;
    setText("credit-balance-available", "—");
    setText("credit-balance-unit", t("credit.balance.unit"));
    setText("credit-balance-kind", "");
    setHidden("credit-reserved-row", true);
    setHidden("credit-refund-row", true);
    setHidden("credit-restricted", true);
    var reasonKey = "acct.balance.reason.network";
    if (status === 503 || code === "credits_unavailable") {
      reasonKey = "acct.balance.reason.db";
    } else if (status === 401 || status === 403) {
      reasonKey = "acct.balance.reason.auth";
    } else if (status === 400 && code === "spend_total_allowance_missing") {
      reasonKey = "acct.balance.reason.missing";
    } else if (status) {
      reasonKey = "acct.balance.reason.http";
    }
    var reason = t(reasonKey, { status: status ? String(status) : "" });
    setMessage("credit-state", t("acct.balance.unavailable", { reason: reason }),
      "error");
    setTopupEnabled(state, false);
  }

  function renderAccount(state, data) {
    state.account = data;
    state.accountError = null;
    var backend = data.billing_backend || "legacy";
    var subject = data.subject || {};
    setText("credit-account-line", subject.username || "");
    setHidden("credit-preview-tag", !subject.preview);
    setText("credit-balance-account", subject.username || "");
    setHidden("credit-reserved-row", true);
    setHidden("credit-refund-row", true);
    setHidden("credit-restricted", true);
    setText("credit-balance-kind", "");
    setText("credit-balance-available", "—");
    setText("credit-balance-unit", t("credit.balance.unit"));

    if (backend === "credits") {
      var wallet = data.wallet || {};
      state.balanceUnits = (wallet.available_units === null ||
        wallet.available_units === undefined)
        ? null : BigInt(String(wallet.available_units));
      var available = formatCreditsUnits(wallet.available_units);
      setText("credit-balance-available", available === null ? "—" : available);
      setText("credit-balance-unit", t("credit.balance.unit"));
      if (isNonZeroUnits(wallet.reserved_units)) {
        setHidden("credit-reserved-row", false);
        setText("credit-reserved",
          formatCreditsUnits(wallet.reserved_units) + " " +
          t("credit.balance.unit"));
      }
      if (isNonZeroUnits(wallet.refund_pending_units)) {
        setHidden("credit-refund-row", false);
        setText("credit-refund-pending",
          formatCreditsUnits(wallet.refund_pending_units) + " " +
          t("credit.balance.unit"));
      }
      if (wallet.restricted_reason) {
        setHidden("credit-restricted", false);
        setText("credit-restricted",
          t("credit.restricted.text", {
            reason: restrictedReasonText(wallet.restricted_reason),
          }));
      }
      state.canTopup = !!data.can_topup;
      state.blockReason = data.topup_reason || null;
    } else if (backend === "owner_budget") {
      state.balanceUnits = null;
      setText("credit-balance-kind", t("credit.balance.owner.title"));
      setText("credit-balance-available",
        formatCnyNano(data.remaining_nano_cny) || "—");
      setText("credit-balance-unit", "CNY");
      state.canTopup = false;
      state.blockReason = "not_user";
    } else {
      state.balanceUnits = null;
      setText("credit-balance-kind", t("credit.balance.legacy.title"));
      setText("credit-balance-available",
        formatCnyNano(data.remaining_nano_cny) || "—");
      setText("credit-balance-unit", "CNY");
      state.canTopup = false;
      state.blockReason = "wallet_not_credits";
    }

    if (state.canTopup) {
      setMessage("credit-state", "", null);
    } else {
      setMessage("credit-state", t(topupBlockedKey(state.blockReason)), "error");
    }
    setTopupEnabled(state, state.canTopup);
    updateCheckout(state);
  }

  function restrictedReasonText(reason) {
    var raw = String(reason || "");
    if (raw.indexOf("account_status_") === 0) {
      return t("credit.restricted.account_status");
    }
    if (raw.indexOf("frozen:") === 0) {
      return t("credit.restricted.frozen", { reason: raw.slice(7) });
    }
    if (raw === "pending_reconciliation") {
      return t("credit.restricted.pending_reconciliation");
    }
    return t("credit.restricted.other", { reason: raw });
  }

  function loadAccount(state) {
    return fetch("/api/account/credits", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        return { status: res.status, body: body || {} };
      });
    }).then(function (res) {
      if (res.status === 200 && res.body && res.body.billing_backend) {
        renderAccount(state, res.body);
      } else {
        renderAccountUnavailable(state, res.status, res.body && res.body.code);
      }
    }).catch(function () {
      renderAccountUnavailable(state, 0, null);
    });
  }

  /* ------------------------------ 套餐/付款 ----------------------------- */
  function packageRadios() {
    var list = $("credit-package-list");
    if (!list || !list.querySelectorAll) return [];
    return Array.prototype.slice.call(
      list.querySelectorAll('input[name="credit-package"]'));
  }

  function selectedPackage(state) {
    var radios = packageRadios();
    for (var i = 0; i < radios.length; i++) {
      if (radios[i].checked) {
        var radio = radios[i];
        return {
          packageId: radio.value,
          packageVersion: String(
            radio.getAttribute("data-package-version") ||
            state.boot.package_version || "1"),
          amountFen: radio.getAttribute("data-amount-fen") || "0",
          creditUnits: radio.getAttribute("data-credit-units") || "0",
        };
      }
    }
    return null;
  }

  function setTopupEnabled(state, enabled) {
    var radios = packageRadios();
    for (var i = 0; i < radios.length; i++) radios[i].disabled = !enabled;
    var payBtn = $("credit-pay-btn");
    if (payBtn) payBtn.disabled = !enabled || !!state.busy;
  }

  function setBusy(state, busy) {
    state.busy = !!busy;
    var label = $("credit-pay-label");
    if (label) {
      label.textContent = busy
        ? t("credit.checkout.busy")
        : t("credit.checkout.pay");
    }
    var arrow = $("credit-pay-arrow");
    if (arrow) arrow.hidden = !!busy;
    setTopupEnabled(state, state.canTopup && !busy);
  }

  function updateCheckout(state) {
    var pkg = selectedPackage(state);
    if (!pkg) return;
    setText("credit-receive", formatCreditsUnits(pkg.creditUnits) || "—");
    setText("credit-amount", formatCnyFen(pkg.amountFen) || "—");
    if (state.balanceUnits !== null && state.balanceUnits !== undefined) {
      var after;
      try {
        after = state.balanceUnits + BigInt(String(pkg.creditUnits));
      } catch (e) {
        after = null;
      }
      setText("credit-after",
        after === null ? "—" : (formatCreditsUnits(after.toString()) || "—"));
    } else {
      setText("credit-after", "—");
    }
  }

  function handleTopupError(state, status, body) {
    var code = body && body.code;
    if (code === "credit_package_stale") {
      state.keys.reset();
      setMessage("credit-state", t("credit.error.stale"), "error");
      try { window.location.reload(); } catch (e) { /* 保持当前页 */ }
      return;
    }
    if (code === "credit_topup_rate_limited" || status === 429) {
      setMessage("credit-state", t("credit.error.rate_limited"), "error");
      return;
    }
    if (code === "credit_topup_unavailable" || status === 503) {
      setMessage("credit-state", t("credit.error.unavailable"), "error");
      return;
    }
    if (code === "credit_topup_not_allowed" || status === 403) {
      setMessage("credit-state", t("credit.error.not_allowed"), "error");
      return;
    }
    if (code === "credit_idempotency_conflict") {
      state.keys.reset();
      setMessage("credit-state", t("credit.error.invalid"), "error");
      return;
    }
    setMessage("credit-state", t("credit.error.invalid"), "error");
  }

  function pay(state) {
    if (!state.canTopup || state.busy) return;
    var pkg = selectedPackage(state);
    if (!pkg) return;
    var signature = pkg.packageId + ":" + pkg.packageVersion;
    var key = state.keys.forAttempt(signature);
    setBusy(state, true);
    setMessage("credit-state", t("credit.state.creating"), null);
    fetch("/api/account/credit-topups", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-CSRF-Token": state.boot.csrf_token,
        "Idempotency-Key": key,
      },
      body: JSON.stringify({
        package_id: pkg.packageId,
        package_version: parseInt(pkg.packageVersion, 10),
      }),
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        return { status: res.status, body: body || {} };
      });
    }).then(function (res) {
      setBusy(state, false);
      if (res.status === 200 && res.body) {
        if (res.body.pay_url) {
          if (isAllowedPayUrl(res.body.pay_url)) {
            window.location.assign(res.body.pay_url);
            return;
          }
          setMessage("credit-state", t("credit.error.pay_url"), "error");
          return;
        }
        if (res.body.order && res.body.order.order_id) {
          // 无支付 URL（渠道暂不可用）：进入订单页查看可恢复状态
          window.location.assign("/app/credits/orders/" +
            encodeURIComponent(res.body.order.order_id));
          return;
        }
      }
      handleTopupError(state, res.status, res.body);
    }).catch(function () {
      setBusy(state, false);
      setMessage("credit-state", t("credit.error.network"), "error");
    });
  }

  /* ------------------------------ 充值记录 ------------------------------ */
  function paymentLabel(order) {
    if (order.payment_state === "paid") return t("credit.state.paid_payment");
    if (order.payment_state === "closed") return t("credit.state.closed");
    return t("credit.state.pending");
  }

  function creditLabel(order) {
    if (order.credit_state === "credited") return t("credit.state.credited");
    if (order.credit_state === "processing") return t("credit.state.processing");
    return t("credit.state.not_credited");
  }

  function paymentClass(order) {
    if (order.payment_state === "paid") return "";
    if (order.payment_state === "closed") return " is-closed";
    return " is-waiting";
  }

  function creditClass(order) {
    if (order.credit_state === "credited") return "";
    if (order.credit_state === "processing") return " is-waiting";
    return " is-unavailable";
  }

  function statusCell(label, cls) {
    var td = el("td");
    td.appendChild(el("span", "credit-status" + (cls || ""), label));
    return td;
  }

  function renderTopups(state) {
    var body = $("credit-topups-body");
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);
    state.topups.forEach(function (order) {
      var tr = el("tr");
      tr.appendChild(el("td", "", formatTime(order.created_at)));
      tr.appendChild(el("td", "", "+" + (formatCreditsUnits(order.credit_units) || "—")));
      tr.appendChild(el("td", "credit-amount-col",
        order.amount_yuan ? "¥" + order.amount_yuan
          : (formatCnyFen(order.amount_fen) || "—")));
      tr.appendChild(statusCell(paymentLabel(order), paymentClass(order)));
      tr.appendChild(statusCell(creditLabel(order), creditClass(order)));
      body.appendChild(tr);
    });
    setHidden("credit-topups-empty", state.topups.length > 0);
  }

  function loadTopups(state, reset) {
    if (state.topupsLoading) return;
    state.topupsLoading = true;
    var cursor = reset ? null : state.topupsCursor;
    var url = "/api/account/credit-topups?limit=10" +
      (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          return { status: res.status, body: body || {} };
        });
      })
      .then(function (res) {
        state.topupsLoading = false;
        if (res.status !== 200) {
          setHidden("credit-topups-more", true);
          return;
        }
        if (reset) state.topups = [];
        state.topups = state.topups.concat(res.body.orders || []);
        state.topupsCursor = res.body.next_cursor || null;
        renderTopups(state);
        setHidden("credit-topups-more", !state.topupsCursor);
      })
      .catch(function () {
        state.topupsLoading = false;
      });
  }

  /* ------------------------------ Credit 明细 --------------------------- */
  function renderLedger(state) {
    var body = $("credit-ledger-body");
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);
    state.ledger.forEach(function (item) {
      var tr = el("tr");
      tr.appendChild(el("td", "", formatTime(item.created_at)));
      tr.appendChild(el("td", "", t(item.label_key || "credit.ledger.other")));
      tr.appendChild(el("td", "", formatSignedUnits(item.amount_units) || "—"));
      var detail = item.model || "";
      if (item.request_id) detail += (detail ? " · " : "") + item.request_id;
      tr.appendChild(el("td", "", detail || "—"));
      body.appendChild(tr);
    });
    setHidden("credit-ledger-empty",
      state.ledger.length > 0 || state.ledgerUnavailable);
  }

  function loadLedger(state, reset) {
    if (state.ledgerLoading) return;
    state.ledgerLoading = true;
    var cursor = reset ? null : state.ledgerCursor;
    var url = "/api/account/credits/ledger?limit=20" +
      (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          return { status: res.status, body: body || {} };
        });
      })
      .then(function (res) {
        state.ledgerLoading = false;
        if (res.status === 409 && res.body.code === "credit_wallet_not_enabled") {
          state.ledgerUnavailable = true;
          setHidden("credit-ledger-unavailable", false);
          setHidden("credit-ledger-more", true);
          renderLedger(state);
          return;
        }
        if (res.status !== 200) {
          setHidden("credit-ledger-more", true);
          return;
        }
        state.ledgerUnavailable = false;
        setHidden("credit-ledger-unavailable", true);
        if (reset) state.ledger = [];
        state.ledger = state.ledger.concat(res.body.items || []);
        state.ledgerCursor = res.body.next_cursor || null;
        renderLedger(state);
        setHidden("credit-ledger-more", !state.ledgerCursor);
      })
      .catch(function () {
        state.ledgerLoading = false;
      });
  }

  /* ------------------------------ 订单结果页 ---------------------------- */
  function orderDetailText(order) {
    var key = orderStateKey(order);
    if (key === "credit.state.credited") {
      return t("credit.state.credited_detail", {
        n: formatCreditsUnits(order.credit_units) || "—",
      });
    }
    if (key === "credit.state.processing") {
      return t("credit.state.processing_detail");
    }
    if (key === "credit.state.pending") {
      return t("credit.state.waiting_payment_detail");
    }
    if (key === "credit.state.closed") {
      return t("credit.state.closed_detail");
    }
    return t("credit.state.unavailable_detail");
  }

  function renderOrder(state) {
    var order = state.order;
    if (!order) return;
    state.orderUnavailable = false;
    var key = orderStateKey(order);
    setText("credit-order-state", t(key));
    setText("credit-order-detail", orderDetailText(order));
    setText("credit-order-id", order.order_id || "—");
    setText("credit-order-credits",
      formatCreditsUnits(order.credit_units) || "—");
    setText("credit-order-amount",
      order.amount_yuan ? "¥" + order.amount_yuan
        : (formatCnyFen(order.amount_fen) || "—"));
    var payLink = $("credit-order-pay-link");
    if (payLink) {
      if (order.pay_url && isAllowedPayUrl(order.pay_url)) {
        payLink.href = order.pay_url;
        payLink.hidden = false;
      } else {
        payLink.removeAttribute("href");
        payLink.hidden = true;
      }
    }
    var stateEl = $("credit-order-state");
    toggleClass(stateEl, "credit-success", key === "credit.state.credited");
  }

  function setOrderUnavailable(state) {
    state.orderUnavailable = true;
    setText("credit-order-state", t("credit.state.unavailable"));
    setText("credit-order-detail", t("credit.state.unavailable_detail"));
    setHidden("credit-order-message", true);
  }

  function initOrder(state) {
    var orderId = state.boot.order_id;
    if (!orderId) {
      setOrderUnavailable(state);
      return;
    }
    var startedAt = Date.now();
    var delay = POLL_START_MS;
    var timer = null;
    var inFlight = false;
    var stopped = false;

    function stop() {
      stopped = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    }

    function schedule() {
      if (stopped) return;
      if (Date.now() - startedAt > POLL_LIMIT_MS) {
        stopped = true;
        setOrderUnavailable(state);
        return;
      }
      delay = Math.min(POLL_MAX_MS, Math.round(delay * 1.5));
      if (timer) clearTimeout(timer);
      timer = setTimeout(poll, delay);
    }

    function poll() {
      // 同一时刻至多一个查询在途：可见性切换触发的提前查询不能另起一条轮询链
      if (stopped || inFlight) return;
      timer = null;
      inFlight = true;
      fetch("/api/account/credit-topups/" + encodeURIComponent(orderId), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (body) {
          return { status: res.status, body: body || {} };
        });
      }).then(function (res) {
        inFlight = false;
        if (stopped) return;
        if (res.status === 200 && res.body && res.body.order) {
          state.order = res.body.order;
          renderOrder(state);
          setHidden("credit-order-message", true);
          if (isTerminalOrder(state.order)) {
            stop();
            if (state.order.credit_state === "credited") loadAccount(state);
            return;
          }
          schedule();
          return;
        }
        if (res.status === 404) {
          stop();
          setOrderUnavailable(state);
          setText("credit-order-detail", t("credit.order.notfound"));
          return;
        }
        // 503/429/网络：显示暂时不可用，但继续轮询（非终态）
        setOrderUnavailable(state);
        schedule();
      }).catch(function () {
        inFlight = false;
        if (stopped) return;
        setOrderUnavailable(state);
        schedule();
      });
    }

    document.addEventListener("visibilitychange", function () {
      if (document.hidden || stopped) return;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      delay = POLL_START_MS;
      poll();
    });

    poll();
  }

  /* ------------------------------ 初始化 -------------------------------- */
  function readBootstrap() {
    var node = $("credits-bootstrap");
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || node.innerHTML || "{}");
    } catch (e) {
      return null;
    }
  }

  function buildState(boot) {
    return {
      boot: boot || {},
      account: null,
      accountError: null,
      order: null,
      orderUnavailable: false,
      canTopup: false,
      blockReason: null,
      busy: false,
      balanceUnits: null,
      keys: new AttemptKeys(window.crypto || null),
      topups: [],
      topupsCursor: null,
      topupsLoading: false,
      ledger: [],
      ledgerCursor: null,
      ledgerLoading: false,
      ledgerUnavailable: false,
    };
  }

  function bindLanguageRerender(state) {
    document.addEventListener("hp-lang-change", function () {
      syncTitle();
      if (state.account) {
        renderAccount(state, state.account);
      } else if (state.accountError) {
        renderAccountUnavailable(state, state.accountError.status,
          state.accountError.code);
      }
      if (state.order) renderOrder(state);
      else if (state.orderUnavailable) setOrderUnavailable(state);
      renderTopups(state);
      renderLedger(state);
      setBusy(state, state.busy);
    });
  }

  function syncTitle() {
    var title = t("credit.doc.title");
    if (title && title !== "credit.doc.title") document.title = title;
  }

  function initTopup(state) {
    var radios = packageRadios();
    for (var i = 0; i < radios.length; i++) {
      radios[i].addEventListener("change", function () {
        updateCheckout(state);
      });
    }
    var payBtn = $("credit-pay-btn");
    if (payBtn) {
      payBtn.addEventListener("click", function () { pay(state); });
    }
    var moreTopups = $("credit-topups-more");
    if (moreTopups) {
      moreTopups.addEventListener("click", function () {
        loadTopups(state, false);
      });
    }
    var moreLedger = $("credit-ledger-more");
    if (moreLedger) {
      moreLedger.addEventListener("click", function () {
        loadLedger(state, false);
      });
    }
    updateCheckout(state);
    loadTopups(state, true);
    loadLedger(state, true);
  }

  function init() {
    var boot = readBootstrap();
    if (!boot) return;
    var state = buildState(boot);
    setText("credit-account-line",
      (boot.subject && boot.subject.username) || "");
    setHidden("credit-preview-tag", !(boot.subject && boot.subject.preview));
    syncTitle();
    bindLanguageRerender(state);
    if (boot.page === "order") initOrder(state);
    else initTopup(state);
    loadAccount(state);
  }

  // 纯函数/状态机挂载点（tests/js/credits-page.test.ts 行为契约）
  window.HP_CREDITS = {
    formatCreditsUnits: formatCreditsUnits,
    formatCnyFen: formatCnyFen,
    formatCnyNano: formatCnyNano,
    formatSignedUnits: formatSignedUnits,
    orderStateKey: orderStateKey,
    isTerminalOrder: isTerminalOrder,
    topupBlockedKey: topupBlockedKey,
    isAllowedPayUrl: isAllowedPayUrl,
    generateKey: generateKey,
    AttemptKeys: AttemptKeys,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
