/* EAP Widget · 嵌入外链 JS SDK（docs/04 §6 / docs/07 §6）
 * 用法一（声明式）： <script src="https://host/sdk/eap-widget.js"></script>
 *                   <eap-chat agent="faq-agent" endpoint="https://host" token="eap_emb_..."></eap-chat>
 * 用法二（可编程）： EAP.widget.mount("#box", {agent, endpoint, session 或 token, height})
 * 安全约定：token 只应出现在内网/开发演示；生产由页面后端换取 session 后注入。
 */
(function () {
  "use strict";

  var STYLE = [
    ":host{--eap-accent:#4f8ef7;--eap-bg:#0f1a2e;--eap-fg:#e6edf7;--eap-sub:#93a4bd;--eap-line:#26385a;",
    "  --eap-user:#2b4c8c;all:initial;display:block;font-family:'Segoe UI','Microsoft YaHei',system-ui,sans-serif;}",
    ".wrap{display:flex;flex-direction:column;background:var(--eap-bg);border:1px solid var(--eap-line);border-radius:12px;overflow:hidden;height:100%;}",
    ".head{padding:10px 14px;font-size:13px;font-weight:600;color:var(--eap-fg);border-bottom:1px solid var(--eap-line);display:flex;gap:8px;align-items:center;}",
    ".head .dot{width:8px;height:8px;border-radius:50%;background:var(--eap-accent);}",
    ".msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;box-sizing:border-box;}",
    ".msg{max-width:85%;padding:8px 12px;border-radius:10px;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;box-sizing:border-box;}",
    ".msg.user{align-self:flex-end;background:var(--eap-user);color:var(--eap-fg);}",
    ".msg.bot{align-self:flex-start;background:#16233d;color:var(--eap-fg);}",
    ".msg .cite{margin-top:6px;font-size:11px;color:var(--eap-sub);}",
    ".msg .cite span{display:inline-block;margin:2px 6px 0 0;padding:1px 6px;border:1px solid var(--eap-line);border-radius:6px;}",
    ".msg.err{background:#3a1620;color:#ff9db0;}",
    ".foot{display:flex;gap:8px;padding:10px;border-top:1px solid var(--eap-line);}",
    "input{flex:1;padding:8px 10px;border-radius:8px;border:1px solid var(--eap-line);background:#0b1425;color:var(--eap-fg);font-size:13px;outline:none;box-sizing:border-box;}",
    "input:focus{border-color:var(--eap-accent);}",
    "button{padding:8px 14px;border-radius:8px;border:none;background:var(--eap-accent);color:#fff;font-size:13px;cursor:pointer;}",
    "button:disabled{opacity:.5;cursor:default;}"
  ].join("");

  function parseSSE(buffer, onEvent) {
    // 解析 "event: x\ndata: {...}\n\n" 帧；返回剩余 buffer
    var frames = buffer.split("\n\n");
    var rest = frames.pop();
    frames.forEach(function (frame) {
      var event = "message", data = "";
      frame.split("\n").forEach(function (line) {
        if (line.indexOf("event:") === 0) event = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
      });
      if (data) {
        try { onEvent(event, JSON.parse(data)); } catch (e) { /* 忽略坏帧 */ }
      }
    });
    return rest;
  }

  class EapChat extends HTMLElement {
    connectedCallback() {
      var self = this;
      var shadow = this.attachShadow({ mode: "open" });
      var style = document.createElement("style");
      style.textContent = STYLE;
      var wrap = document.createElement("div");
      wrap.className = "wrap";
      wrap.style.height = this.getAttribute("height") || "420px";

      var head = document.createElement("div");
      head.className = "head";
      var dot = document.createElement("span");
      dot.className = "dot";
      var title = document.createElement("span");
      title.textContent = this.getAttribute("title") || ("智能体 · " + (this.getAttribute("agent") || ""));
      head.appendChild(dot);
      head.appendChild(title);

      var msgs = document.createElement("div");
      msgs.className = "msgs";
      var foot = document.createElement("div");
      foot.className = "foot";
      var input = document.createElement("input");
      input.placeholder = "输入消息，回车发送…";
      var btn = document.createElement("button");
      btn.textContent = "发送";
      foot.appendChild(input);
      foot.appendChild(btn);
      wrap.appendChild(head);
      wrap.appendChild(msgs);
      wrap.appendChild(foot);
      shadow.appendChild(style);
      shadow.appendChild(wrap);

      this._els = { msgs: msgs, input: input, btn: btn };
      var send = function () {
        var v = input.value.trim();
        if (!v) return;
        input.value = "";
        self.send(v);
      };
      btn.addEventListener("click", send);
      input.addEventListener("keydown", function (e) { if (e.key === "Enter") send(); });

      this._session = this.getAttribute("session") || "";
      if (this._session) {
        this.welcome();
      } else {
        this.exchange().catch(function (e) {
          self.addMsg("err", "会话建立失败：" + (e && e.message ? e.message : e));
        });
      }
    }

    attr(name, fallback) {
      var v = this.getAttribute(name);
      return v === null || v === "" ? (fallback || "") : v;
    }

    exchange() {
      var self = this;
      var endpoint = this.attr("endpoint", location.origin);
      var token = this.attr("token");
      if (!token) return Promise.reject(new Error("缺少 token 或 session 属性"));
      return fetch(endpoint + "/api/v1/embed/session", {
        method: "POST",
        headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: this.attr("user-id") })
      }).then(function (r) {
        if (!r.ok) return r.json().catch(function () { return {}; }).then(function (d) {
          throw new Error(d.detail || ("HTTP " + r.status));
        });
        return r.json();
      }).then(function (d) {
        self._session = d.session_token;
        self.welcome();
        return d.session_token;
      });
    }

    welcome() {
      this.addMsg("bot", "你好，我是 " + this.attr("agent") + "，请问有什么可以帮你？");
    }

    addMsg(cls, text, cites) {
      var msgs = this._els.msgs;
      var el = document.createElement("div");
      el.className = "msg " + cls;
      el.textContent = text;
      if (cites && cites.length) {
        var c = document.createElement("div");
        c.className = "cite";
        c.appendChild(document.createTextNode("来源："));
        cites.forEach(function (x) {
          var s = document.createElement("span");
          s.textContent = (x.document || x.kb || "") + " #" + (x.chunk_index || 0);
          c.appendChild(s);
        });
        el.appendChild(c);
      }
      msgs.appendChild(el);
      msgs.scrollTop = msgs.scrollHeight;
      return el;
    }

    send(text) {
      var self = this;
      if (!text.trim()) return;
      this.addMsg("user", text);
      var bubble = this.addMsg("bot", "…");
      var endpoint = this.attr("endpoint", location.origin);
      var agent = this.attr("agent");
      this._els.btn.disabled = true;

      var doRequest = function (session) {
        return fetch(endpoint + "/api/v1/agents/" + encodeURIComponent(agent) + "/invocations", {
          method: "POST",
          headers: { "Authorization": "Bearer " + session, "Content-Type": "application/json" },
          body: JSON.stringify({ input: text, stream: true })
        });
      };

      var finish = function (data) {
        bubble.textContent = data.output || "（空回复）";
        if (data.citations && data.citations.length) {
          var c = document.createElement("div");
          c.className = "cite";
          c.appendChild(document.createTextNode("来源："));
          data.citations.forEach(function (x) {
            var s = document.createElement("span");
            s.textContent = (x.document || "") + " #" + (x.chunk_index || 0);
            c.appendChild(s);
          });
          bubble.appendChild(c);
        }
        self._els.btn.disabled = false;
      };

      var p = this._session ? Promise.resolve(this._session) : this.exchange();
      p.then(doRequest).then(function (resp) {
        if (!resp.ok || !resp.body) {
          return resp.json().catch(function () { return {}; }).then(function (d) {
            throw new Error(d.detail || ("HTTP " + resp.status));
          });
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) { self._els.btn.disabled = false; return; }
            buffer = parseSSE(buffer + decoder.decode(r.value, { stream: true }), function (event, data) {
              if (event === "step") bubble.textContent = data.step;
              else if (event === "result") finish(data);
              else if (event === "error") {
                bubble.className = "msg err";
                bubble.textContent = data.message;
                self._els.btn.disabled = false;
              }
            });
            return pump();
          });
        }
        return pump();
      }).catch(function (e) {
        bubble.className = "msg err";
        bubble.textContent = "调用失败：" + (e && e.message ? e.message : e);
        self._els.btn.disabled = false;
      });
    }
  }

  if (window.customElements && !customElements.get("eap-chat")) {
    customElements.define("eap-chat", EapChat);
  }

  window.EAP = window.EAP || {};
  window.EAP.widget = {
    mount: function (selector, opts) {
      opts = opts || {};
      var host = typeof selector === "string" ? document.querySelector(selector) : selector;
      var el = document.createElement("eap-chat");
      ["agent", "endpoint", "token", "session", "height", "title", "user-id"].forEach(function (k) {
        if (opts[k] !== undefined) el.setAttribute(k, opts[k]);
      });
      host.appendChild(el);
      return el;
    }
  };
})();
