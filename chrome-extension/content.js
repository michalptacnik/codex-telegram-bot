// AgentHQ Content Script — DOM interaction layer
// Injected into every page to provide seamless click, type, fill,
// hover, press, scroll, select, wait, and get_text actions.
// Inspired by OpenClaw's browser relay content script pattern.

(() => {
  "use strict";

  // ── Helpers ──────────────────────────────────────────────────────

  /**
   * Resolve an element from a selector string or numeric snapshot ref.
   * Accepts CSS selectors, "#id", or a stringified ref number that maps
   * to a selector_path captured by the snapshot command.
   */
  function resolveElement(selectorOrRef) {
    if (!selectorOrRef) return null;
    const text = String(selectorOrRef).trim();
    if (!text) return null;

    // Direct CSS selector
    try {
      const el = document.querySelector(text);
      if (el) return el;
    } catch (_e) {
      // Not a valid CSS selector — fall through
    }

    // Numeric ref: try data attribute set by snapshot, or aria fallback
    if (/^\d+$/.test(text)) {
      const byData = document.querySelector(`[data-agenthq-ref="${text}"]`);
      if (byData) return byData;
    }

    return null;
  }

  /**
   * Scroll an element into view if it isn't already visible.
   */
  function ensureVisible(el) {
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const inView =
      rect.top >= 0 &&
      rect.left >= 0 &&
      rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
      rect.right <= (window.innerWidth || document.documentElement.clientWidth);
    if (!inView) {
      el.scrollIntoView({ behavior: "instant", block: "center", inline: "center" });
    }
  }

  /**
   * Dispatch a sequence of mouse events on an element.
   */
  function dispatchMouseEvents(el, eventNames, opts) {
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const base = {
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: cx,
      clientY: cy,
      ...(opts || {})
    };
    for (const name of eventNames) {
      el.dispatchEvent(new MouseEvent(name, base));
    }
  }

  /**
   * Dispatch keyboard events on an element.
   */
  function dispatchKeyEvent(el, eventName, key, code) {
    el.dispatchEvent(
      new KeyboardEvent(eventName, {
        key: key,
        code: code || key,
        bubbles: true,
        cancelable: true,
        view: window
      })
    );
  }

  /**
   * Focus an element, trying multiple strategies.
   */
  function focusElement(el) {
    if (typeof el.focus === "function") {
      el.focus();
    }
    // For contenteditable elements or divs acting as inputs
    if (document.activeElement !== el) {
      el.dispatchEvent(new FocusEvent("focus", { bubbles: true }));
      el.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    }
  }

  // ── Action handlers ──────────────────────────────────────────────

  function handleClick(payload) {
    const el = resolveElement(payload.selector);
    if (!el) {
      return { ok: false, error: `Element not found: ${payload.selector}` };
    }
    ensureVisible(el);
    focusElement(el);
    dispatchMouseEvents(el, ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]);
    return { ok: true, output: `Clicked: ${payload.selector}` };
  }

  function handleFill(payload) {
    const el = resolveElement(payload.selector);
    if (!el) {
      return { ok: false, error: `Element not found: ${payload.selector}` };
    }
    ensureVisible(el);
    focusElement(el);

    const value = String(payload.value ?? "");
    const tag = (el.tagName || "").toUpperCase();
    const isContentEditable =
      el.hasAttribute("contenteditable") && el.getAttribute("contenteditable") !== "false";

    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      // Use native setter to bypass React/Vue controlled component guards
      const nativeDescriptor =
        Object.getOwnPropertyDescriptor(
          tag === "SELECT" ? HTMLSelectElement.prototype : (tag === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype),
          "value"
        );
      if (nativeDescriptor && nativeDescriptor.set) {
        nativeDescriptor.set.call(el, value);
      } else {
        el.value = value;
      }
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else if (isContentEditable) {
      // Strategy 1: execCommand selectAll+delete+insertText (works in most browsers).
      // Strategy 2: DataTransfer paste simulation (fallback for Chrome 146+ where
      // execCommand("insertText") may silently fail on React/Draft.js editors).
      // After each attempt, verify the text was actually set by reading textContent.
      el.focus();

      // Use beforeinput + input events — Draft.js v0.14+ (X/Twitter) handles
      // these natively. execCommand("insertText") is unreliable in Chrome 146+.
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
      // Clear
      el.dispatchEvent(new InputEvent("beforeinput", {
        inputType: "deleteContentBackward",
        bubbles: true,
        cancelable: true
      }));
      // Insert
      el.dispatchEvent(new InputEvent("beforeinput", {
        inputType: "insertText",
        data: value,
        bubbles: true,
        cancelable: true
      }));
      el.dispatchEvent(new InputEvent("input", {
        inputType: "insertText",
        data: value,
        bubbles: true
      }));
      // Fallback: if beforeinput left field empty, try execCommand
      const textAfter = (el.textContent || el.innerText || "").trim();
      if (!textAfter) {
        sel.removeAllRanges();
        const r2 = document.createRange();
        r2.selectNodeContents(el);
        sel.addRange(r2);
        document.execCommand("delete", false, null);
        document.execCommand("insertText", false, value);
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    } else {
      return { ok: false, error: `Element is not fillable: ${payload.selector}` };
    }

    return { ok: true, output: `Filled: ${payload.selector}` };
  }

  function handleType(payload) {
    const el = resolveElement(payload.selector);
    if (!el) {
      return { ok: false, error: `Element not found: ${payload.selector}` };
    }
    ensureVisible(el);
    focusElement(el);

    const text = String(payload.text ?? "");
    const isContentEditable =
      el.hasAttribute("contenteditable") && el.getAttribute("contenteditable") !== "false";

    if (isContentEditable) {
      // Chrome 86+ / React 16+ Draft.js editors no longer respond reliably to
      // execCommand("insertText"). Instead, dispatch beforeinput + input events
      // with inputType="insertText" which Draft.js v0.14+ handles natively.
      //
      // Step 1: clear existing content via selection + deleteContent beforeinput
      el.focus();
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
      el.dispatchEvent(new InputEvent("beforeinput", {
        inputType: "deleteContentBackward",
        bubbles: true,
        cancelable: true
      }));
      // Step 2: insert the full text in one beforeinput event
      el.dispatchEvent(new InputEvent("beforeinput", {
        inputType: "insertText",
        data: text,
        bubbles: true,
        cancelable: true
      }));
      el.dispatchEvent(new InputEvent("input", {
        inputType: "insertText",
        data: text,
        bubbles: true
      }));
      // Step 3: fallback — if beforeinput approach left content empty, try execCommand
      const inserted = (el.textContent || el.innerText || "").trim();
      if (!inserted) {
        sel.removeAllRanges();
        const r2 = document.createRange();
        r2.selectNodeContents(el);
        sel.addRange(r2);
        document.execCommand("delete", false, null);
        document.execCommand("insertText", false, text);
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    } else {
      // For regular inputs, type char by char with proper events
      for (const char of text) {
        dispatchKeyEvent(el, "keydown", char, "");
        dispatchKeyEvent(el, "keypress", char, "");

        // Append to current value
        const nativeDescriptor =
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value") ||
          Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");
        if (nativeDescriptor && nativeDescriptor.set) {
          nativeDescriptor.set.call(el, el.value + char);
        } else {
          el.value += char;
        }
        el.dispatchEvent(new InputEvent("input", { bubbles: true, data: char, inputType: "insertText" }));
        dispatchKeyEvent(el, "keyup", char, "");
      }
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }

    return { ok: true, output: `Typed ${text.length} chars into: ${payload.selector}` };
  }

  function handleHover(payload) {
    const el = resolveElement(payload.selector);
    if (!el) {
      return { ok: false, error: `Element not found: ${payload.selector}` };
    }
    ensureVisible(el);
    dispatchMouseEvents(el, ["pointerenter", "pointerover", "mouseenter", "mouseover", "pointermove", "mousemove"]);
    return { ok: true, output: `Hovered: ${payload.selector}` };
  }

  function handlePress(payload) {
    const key = String(payload.key || "");
    if (!key) {
      return { ok: false, error: "Missing key" };
    }

    // Target: focused element or body
    const target = document.activeElement || document.body;

    // Map common key names to proper KeyboardEvent values
    const KEY_MAP = {
      "enter": { key: "Enter", code: "Enter" },
      "tab": { key: "Tab", code: "Tab" },
      "escape": { key: "Escape", code: "Escape" },
      "esc": { key: "Escape", code: "Escape" },
      "backspace": { key: "Backspace", code: "Backspace" },
      "delete": { key: "Delete", code: "Delete" },
      "space": { key: " ", code: "Space" },
      "arrowup": { key: "ArrowUp", code: "ArrowUp" },
      "arrowdown": { key: "ArrowDown", code: "ArrowDown" },
      "arrowleft": { key: "ArrowLeft", code: "ArrowLeft" },
      "arrowright": { key: "ArrowRight", code: "ArrowRight" },
      "home": { key: "Home", code: "Home" },
      "end": { key: "End", code: "End" },
      "pageup": { key: "PageUp", code: "PageUp" },
      "pagedown": { key: "PageDown", code: "PageDown" },
    };

    const mapped = KEY_MAP[key.toLowerCase()] || { key: key, code: key };

    dispatchKeyEvent(target, "keydown", mapped.key, mapped.code);
    dispatchKeyEvent(target, "keypress", mapped.key, mapped.code);
    dispatchKeyEvent(target, "keyup", mapped.key, mapped.code);

    return { ok: true, output: `Pressed key: ${key}` };
  }

  function handleScroll(payload) {
    const direction = String(payload.direction || "down").toLowerCase();
    const pixels = Number(payload.pixels || 400);

    let dx = 0;
    let dy = 0;
    switch (direction) {
      case "up": dy = -pixels; break;
      case "down": dy = pixels; break;
      case "left": dx = -pixels; break;
      case "right": dx = pixels; break;
      default:
        return { ok: false, error: `Unknown scroll direction: ${direction}` };
    }

    window.scrollBy({ left: dx, top: dy, behavior: "instant" });
    return { ok: true, output: `Scrolled ${direction} ${pixels}px` };
  }

  function handleSelect(payload) {
    const el = resolveElement(payload.selector);
    if (!el) {
      return { ok: false, error: `Element not found: ${payload.selector}` };
    }

    const tag = (el.tagName || "").toUpperCase();
    if (tag !== "SELECT") {
      return { ok: false, error: `Element is not a <select>: ${payload.selector}` };
    }

    const values = Array.isArray(payload.values) ? payload.values : [String(payload.value ?? "")];

    for (const option of el.options) {
      option.selected = values.includes(option.value) || values.includes(option.textContent.trim());
    }

    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, output: `Selected: ${values.join(", ")}` };
  }

  function handleWait(payload) {
    // wait is async — resolved in the message handler below
    const selector = payload.selector;
    const ms = Number(payload.ms || 0);
    const text = payload.text;

    if (selector) {
      // Wait for element to appear
      return new Promise((resolve) => {
        const deadline = Date.now() + (Number(payload.timeout || 10000));

        function check() {
          const el = resolveElement(selector);
          if (el) {
            resolve({ ok: true, output: `Element found: ${selector}` });
            return;
          }
          if (Date.now() >= deadline) {
            resolve({ ok: false, error: `Timeout waiting for: ${selector}` });
            return;
          }
          setTimeout(check, 200);
        }
        check();
      });
    }

    if (text) {
      return new Promise((resolve) => {
        const deadline = Date.now() + (Number(payload.timeout || 10000));

        function check() {
          const bodyText = (document.body.innerText || document.body.textContent || "");
          if (bodyText.includes(text)) {
            resolve({ ok: true, output: `Text found: "${text}"` });
            return;
          }
          if (Date.now() >= deadline) {
            resolve({ ok: false, error: `Timeout waiting for text: "${text}"` });
            return;
          }
          setTimeout(check, 200);
        }
        check();
      });
    }

    if (ms > 0) {
      return new Promise((resolve) => {
        setTimeout(() => {
          resolve({ ok: true, output: `Waited ${ms}ms` });
        }, Math.min(ms, 30000));
      });
    }

    return { ok: true, output: "No wait condition specified" };
  }

  function handleGetText(payload) {
    const el = resolveElement(payload.selector);
    if (!el) {
      return { ok: false, error: `Element not found: ${payload.selector}` };
    }
    const text = (el.innerText || el.textContent || "").trim();
    return { ok: true, output: text, data: { text: text } };
  }

  // ── Message listener ─────────────────────────────────────────────

  const ACTION_HANDLERS = {
    click: handleClick,
    fill: handleFill,
    type: handleType,
    hover: handleHover,
    press: handlePress,
    scroll: handleScroll,
    select: handleSelect,
    wait: handleWait,
    get_text: handleGetText,
  };

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || message.source !== "agenthq-bridge") {
      return false;
    }

    const action = String(message.action || "");
    const handler = ACTION_HANDLERS[action];
    if (!handler) {
      sendResponse({ ok: false, error: `Unknown content action: ${action}` });
      return false;
    }

    try {
      const result = handler(message.payload || {});

      // Handle async (Promise) results
      if (result && typeof result.then === "function") {
        result
          .then((res) => sendResponse(res))
          .catch((err) => sendResponse({ ok: false, error: String(err) }));
        return true; // Keep channel open for async response
      }

      sendResponse(result);
    } catch (err) {
      sendResponse({ ok: false, error: String(err && err.message ? err.message : err) });
    }

    return false;
  });
})();
