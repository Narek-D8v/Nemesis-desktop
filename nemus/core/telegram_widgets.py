"""HTML-панель задачи для Telegram-виджетов (Bot API 10.4).

HTML панели — статическая строка: пользовательские данные (название задачи,
имена инструментов, ошибка) приходят только через ``chat_data`` и выводятся
в DOM через ``textContent``. Поэтому модель или имя файла не могут внедрить
разметку или скрипт.

Состояние компактное (ключи в одну букву) и всегда укладывается в 1024 байта
UTF-8: ``truncate_state`` — часть контракта, а не ошибка.
"""

from __future__ import annotations

import json
import time
from typing import Any

#: не чаще одного edit на панель
WIDGET_INTERVAL = 1.5
#: сколько ждём перед созданием панели: короткие ответы её не создают
PANEL_DELAY = 1.5

#: подпись сообщения-панели
WIDGET_CAPTION = "Панель Немуси"

#: предел chat_data, байт UTF-8
STATE_LIMIT = 1024

#: статическая страница: ни внешних ресурсов, ни fetch, ни innerHTML
WIDGET_HTML = """<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Немуся</title><style>
:root{--bg:#111827;--fg:#e5e7eb;--muted:#9ca3af;--card:#1f2937;--accent:#60a5fa;--ok:#34d399;--err:#f87171}
*{box-sizing:border-box}body{margin:0;padding:10px;font:14px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
.card{background:var(--card);border-radius:12px;padding:12px}
.top{display:flex;align-items:center;gap:8px}.name{font-weight:600}.chip{margin-left:auto;font-size:12px;color:var(--accent)}
.task{color:var(--muted);margin:6px 0 8px;white-space:pre-wrap;word-break:break-word}
.timer{font-variant-numeric:tabular-nums;color:var(--accent);font-size:12px}
#steps{display:flex;flex-wrap:wrap;gap:4px;list-style:none;padding:0;margin:6px 0}
#steps li{background:#374151;border-radius:999px;padding:2px 8px;font-size:12px}
.err{color:var(--err);font-size:12px;margin-top:6px}
.btns{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
button{background:#374151;color:var(--fg);border:0;border-radius:8px;padding:6px 10px;font-size:12px}
button[data-act=stop]{background:#7f1d1d}button[data-act=retry]{background:#1e3a5f}
#say{color:var(--ok);font-size:12px;margin-top:6px;min-height:16px}
</style></head><body><div class="card">
<div class="top"><span class="name">Немуся</span><span id="status" class="chip"></span><span id="timer" class="timer">0:00</span></div>
<div id="title" class="task"></div><ul id="steps"></ul><div id="err" class="err"></div>
<div class="btns">
<button data-act="stop">⏹ Стоп</button><button data-act="status">ℹ️ Статус</button>
<button data-act="new">🆕 Новый</button><button data-act="retry">🔁 Повторить</button>
<button data-act="help">❓ Помощь</button>
</div><div id="say"></div></div>
<script>
(function(){
 var state={v:1,id:0,s:"idle",t:"",steps:[],start:0,end:0,err:"",retry:false};
 var labels={idle:"свободна",working:"работает",done:"готово",error:"ошибка",stopped:"остановлено",stopping:"останавливаю…"};
 function el(id){return document.getElementById(id)}
 function text(id,value){el(id).textContent=value||""}
 function render(raw){
  var next; try{next=JSON.parse(raw||"{}")}catch(e){return}
  if(!next||typeof next!=="object")return;
  state=next;
  text("status",labels[state.s]||state.s||"");
  text("title",state.t||"");
  text("err",state.err||"");
  var list=el("steps"); while(list.firstChild){list.removeChild(list.firstChild)}
  var steps=state.steps||[];
  for(var i=0;i<steps.length;i++){var li=document.createElement("li");li.textContent=steps[i];list.appendChild(li)}
  tick();
 }
 function tick(){
  var end=state.end||0,start=state.start||0,seconds=0;
  if(start){seconds=Math.max(0,Math.round((end||Date.now()/1000)-start))}
  var m=Math.floor(seconds/60),s=seconds%60;
  text("timer",m+":"+(s<10?"0":"")+s);
 }
 function say(message){text("say",message||"")}
 function request(data){
  say("");
  TelegramWidget.request(data).then(function(reply){say(reply&&reply.result?reply.result:"")})
  .catch(function(error){say(error&&error.code?error.code:"FAILED")});
 }
 var buttons=document.querySelectorAll("button[data-act]");
 for(var i=0;i<buttons.length;i++){
  (function(button){
   button.addEventListener("click",function(){
    var act=button.getAttribute("data-act");
    request(act==="stop"||act==="retry"?act+":"+state.id:act);
   });
  })(buttons[i]);
 }
 TelegramWidget.onEvent("chatDataChanged",function(event){render(event&&event.chatData)});
 TelegramWidget.onEvent("init",function(event){render(event&&event.chatData)});
 render(TelegramWidget.chatData||"{}");
 setInterval(tick,1000);
})();
</script></body></html>"""

#: порядок ключей при сериализации — короткий JSON
_STATE_ORDER = ("v", "id", "s", "t", "steps", "start", "end", "err", "retry")


def make_state(
    *,
    task_id: int,
    status: str,
    title: str,
    steps: list[str],
    start: float,
    end: float = 0.0,
    error: str = "",
    retry: bool = False,
) -> dict[str, Any]:
    """Компактное состояние панели по контракту."""
    return {
        "v": 1,
        "id": int(task_id),
        "s": str(status),
        "t": str(title or "")[:120],
        "steps": [str(step)[:40] for step in list(steps or [])[:6]],
        "start": round(float(start or 0.0), 1),
        "end": round(float(end or 0.0), 1),
        "err": str(error or "")[:200],
        "retry": bool(retry),
    }


def state_json(state: dict[str, Any]) -> str:
    """Compact JSON с кириллицей; результат гарантированно ≤1024 байт."""
    ordered = {key: state.get(key) for key in _STATE_ORDER if key in state}
    for key, value in state.items():
        if key not in ordered:
            ordered[key] = value
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def truncate_state(state: dict[str, Any]) -> dict[str, Any]:
    """Убирает лишние шаги/ошибку, пока JSON не влезет в 1024 байта."""
    out = dict(state)
    out["steps"] = list(out.get("steps") or [])[:6]
    while len(state_json(out).encode("utf-8")) > STATE_LIMIT:
        if out.get("err"):
            out["err"] = str(out["err"])[:-20]
            continue
        if out.get("steps"):
            out["steps"] = list(out["steps"])[1:]
            continue
        if len(str(out.get("t") or "")) > 8:
            out["t"] = str(out["t"])[:-20]
            continue
        break
    data = state_json(out)
    if len(data.encode("utf-8")) > STATE_LIMIT:
        out["t"] = ""
        out["steps"] = []
        out["err"] = ""
        data = state_json(out)
    if len(data.encode("utf-8")) > STATE_LIMIT:
        out = {"v": 1, "id": int(out.get("id") or 0), "s": str(out.get("s") or "")}
    return out


def parse_widget_action(data: str) -> tuple[str, int | None]:
    """Разбирает недоверенную строку кнопки: ``stop:7`` или ``status``."""
    raw = (data or "").strip()
    if not raw:
        return "", None
    action, sep, value = raw.partition(":")
    if sep and value.strip().isdigit():
        return action, int(value.strip())
    return raw, None


class WidgetPanel:
    """Одно сообщение-панель на чат и его состояние."""

    def __init__(self, message_id: int, state: dict[str, Any]) -> None:
        self.message_id = message_id
        self.state = state
        self.broken = False
        self._last = 0.0

    @classmethod
    async def create(
        cls,
        api: Any,
        *,
        chat_id: int,
        task_id: int,
        title: str,
    ) -> "WidgetPanel | None":
        state = make_state(
            task_id=task_id,
            status="working",
            title=title,
            steps=[],
            start=time.time(),
        )
        message_id = await api.send_widget(
            chat_id=chat_id,
            html=WIDGET_HTML,
            chat_data=state_json(truncate_state(state)),
            caption=WIDGET_CAPTION,
            allow_queries=True,
        )
        if message_id is None:
            return None
        panel = cls(message_id, state)
        panel._last = time.monotonic()
        return panel

    async def update(
        self,
        api: Any,
        *,
        chat_id: int,
        force: bool = False,
        **changes: Any,
    ) -> None:
        """Обновляет состояние; сеть — не чаще ``WIDGET_INTERVAL``."""
        self.state.update(changes)
        if self.broken:
            return
        now = time.monotonic()
        if not force and now - self._last < WIDGET_INTERVAL:
            return
        self.state = truncate_state(self.state)
        try:
            ok = await api.edit_message_widget_data(
                chat_id=chat_id,
                message_id=self.message_id,
                chat_data=state_json(self.state),
            )
        except Exception:  # noqa: BLE001
            self.broken = True
            return
        if not ok:
            self.broken = True
            return
        self._last = now

    async def close(
        self,
        api: Any,
        *,
        chat_id: int,
        status: str,
        error: str = "",
        retry: bool = False,
    ) -> None:
        """Финальное состояние панели: готово/ошибка/остановлено."""
        await self.update(
            api,
            chat_id=chat_id,
            force=True,
            s=status,
            err=error,
            retry=retry,
            end=time.time(),
        )


__all__ = [
    "PANEL_DELAY",
    "STATE_LIMIT",
    "WIDGET_CAPTION",
    "WIDGET_HTML",
    "WIDGET_INTERVAL",
    "WidgetPanel",
    "make_state",
    "parse_widget_action",
    "state_json",
    "truncate_state",
]
