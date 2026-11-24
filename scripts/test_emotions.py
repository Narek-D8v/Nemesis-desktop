"""Тесты характера и эмоционального состояния Немуси."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nemus.config import Config  # noqa: E402
from nemus.core.emotions import (  # noqa: E402
    DEFAULT_WARMTH,
    EmotionManager,
    EmotionStore,
    current_attachment,
    followup_minutes,
    prompt_block,
    summary_html,
    summary_line,
    summary_text,
)


def _cfg(**updates):
    cfg = Config()
    cfg.character = cfg.character.model_copy(update=updates)
    return cfg


def test_state_changes_and_persists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "emotions.json"
        store = EmotionStore(path)
        cfg = _cfg()
        store.observe_user("Спасибо, ты умница!", cfg.character, now=1000.0)
        state = store.snapshot()
        assert state.mood > cfg.character.base_mood, state
        assert state.last_user_at == 1000.0
        assert state.warmth > DEFAULT_WARMTH, state
        assert state.attachment > cfg.character.attachment, state

        store.observe_user("Ты дура и бесишь", cfg.character, now=1100.0)
        hurt = store.snapshot()
        assert hurt.hurt > 0 and hurt.mood < state.mood, hurt
        assert hurt.conflict_since == 1100.0
        assert hurt.repair_progress == 0
        assert hurt.warmth < state.warmth, (state.warmth, hurt.warmth)
        assert hurt.attachment < state.attachment, (
            state.attachment,
            hurt.attachment,
        )

        store.observe_user("Прости, я не хотел", cfg.character, now=1200.0)
        sorry = store.snapshot()
        assert sorry.hurt < hurt.hurt, sorry
        assert sorry.repair_progress > 0 and sorry.conflict_since == 1100.0
        assert sorry.warmth > hurt.warmth, (hurt.warmth, sorry.warmth)
        assert sorry.attachment > hurt.attachment, (
            hurt.attachment,
            sorry.attachment,
        )

        reloaded = EmotionStore(path).snapshot()
        assert reloaded.last_user_text == "Прости, я не хотел"
    print("ok  эмоции: реплики меняют чувства и переживают перезапуск")


def test_sensitivity_changes_hurt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "emotions.json"
        low = _cfg(sensitivity=10)
        high = _cfg(sensitivity=90)
        EmotionStore(path).observe_user("Ты дура", low.character, now=1000.0)
        low_hurt = EmotionStore(path).snapshot().hurt
        EmotionStore(path).observe_user("Ты дура", high.character, now=1100.0)
        high_hurt = EmotionStore(path).snapshot().hurt
        assert high_hurt > low_hurt, (low_hurt, high_hurt)
    print("ok  эмоции: обидчивость усиливает реакцию на грубость")


def test_proactive_grows_from_loneliness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(quiet_hours_start="00:00", quiet_hours_end="00:00")
        store = EmotionStore(Path(tmp) / "emotions.json")
        store.observe_user("привет", cfg.character, now=1000.0)
        # получасовой тишины мало: скука должна вырасти естественно
        early = 1000.0 + 30 * 60
        store.tick(cfg.character, now=early)
        assert not store.should_reach_out(cfg.character, now=early)
        later = 1000.0 + 3 * 3600
        store.tick(cfg.character, now=later)
        assert store.should_reach_out(cfg.character, now=later)
        store.mark_proactive(text="Тишина какая-то. Я рядом.", now=later)
        assert store.snapshot().last_proactive_text.startswith("Тишина")
        # пока не ответили, она выдерживает паузу, а не пишет каждую минуту
        assert not store.should_reach_out(cfg.character, now=later + 3600)
        store.observe_user("прости, был занят", cfg.character, now=later + 4000)
        assert store.snapshot().awaiting_since == 0
    print("ok  эмоции: инициатива рождается из скуки, а не из расписания")


def test_followup_has_no_message_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(quiet_hours_start="00:00", quiet_hours_end="00:00")
        store = EmotionStore(Path(tmp) / "emotions.json")
        store.observe_user("привет", cfg.character, now=1000.0)
        first = 1000.0 + 3 * 3600
        store.tick(cfg.character, now=first)
        assert store.should_reach_out(cfg.character, now=first)
        store.mark_proactive(text="Ты тут?", now=first)
        calm_pause = followup_minutes(cfg.character, store.snapshot())

        # пауза зависит от скуки и настроения: чем хуже, тем раньше
        early = first + 40 * 60
        store.tick(cfg.character, now=early)
        assert not store.should_reach_out(cfg.character, now=early)
        second = first + 60 * 60
        state = store.tick(cfg.character, now=second)
        assert state.loneliness > 60
        assert followup_minutes(cfg.character, state) < calm_pause
        assert store.should_reach_out(cfg.character, now=second)
        store.mark_proactive(text="Я всё ещё здесь.", now=second)

        # жёсткого лимита нет: после новой паузы она может написать ещё раз
        third = second + 60 * 60
        store.tick(cfg.character, now=third)
        assert store.should_reach_out(cfg.character, now=third)
        store.mark_proactive(text="Ну где ты?", now=third)

        store.observe_user("прости, был занят", cfg.character, now=third + 300)
        assert store.snapshot().awaiting_since == 0
    print("ok  эмоции: число follow-up не ограничено кодом")


def test_hurt_reason_is_recorded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(quiet_hours_start="00:00", quiet_hours_end="00:00")
        store = EmotionStore(Path(tmp) / "emotions.json")
        store.observe_user("Ты дура и бесишь", cfg.character, now=1000.0)
        insulted = store.snapshot()
        assert "Ты дура" in insulted.hurt_reason, insulted.hurt_reason
        assert "Ты дура" in prompt_block(cfg, insulted)

        store.observe_user("Прости, я не хотел", cfg.character, now=1100.0)
        assert store.snapshot().hurt_reason == ""

        store.observe_user("привет", cfg.character, now=1200.0)
        store.mark_proactive(text="Соскучилась.", now=1200.0 + 3 * 3600)
        store.tick(cfg.character, now=1200.0 + 3 * 3600 + 40 * 60)
        ignored = store.snapshot()
        assert "не ответили" in ignored.hurt_reason, ignored.hurt_reason
        block = prompt_block(cfg, ignored)
        assert "не ответили" in block
        assert "не выдумывай" in block
    print("ok  эмоции: причина обиды запоминается, а не придумывается")


def test_explanations_do_not_count_as_insults() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg()
        store = EmotionStore(Path(tmp) / "emotions.json")
        for moment, text in enumerate(
            (
                "Эта ошибка хуже, чем вчера",
                "Эта функция не нужна",
                "Ты не так поняла, я объясняю",
            ),
            start=1000,
        ):
            store.observe_user(text, cfg.character, now=float(moment))
            state = store.snapshot()
            assert state.hurt == 0, (text, state.hurt)
            assert state.hurt_reason == "", (text, state.hurt_reason)

        # настоящая адресная грубость по-прежнему обижает
        store.observe_user("Ты дура и бесишь", cfg.character, now=1100.0)
        assert store.snapshot().hurt > 0
    print("ok  эмоции: объяснения не принимаются за оскорбления")


def test_explanation_softens_ignored_hurt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
            base_hurt=0,
        )
        store = EmotionStore(Path(tmp) / "emotions.json")
        store.observe_user("привет", cfg.character, now=1000.0)
        store.mark_proactive(text="Ты тут?", now=1000.0 + 3 * 3600)
        ignored = 1000.0 + 3 * 3600 + 100 * 60
        store.tick(cfg.character, now=ignored)
        before = store.snapshot()
        assert before.hurt > 55, before.hurt

        store.observe_user(
            "Я не мог ответить, был занят на работе",
            cfg.character,
            now=ignored + 300,
        )
        after = store.snapshot()
        assert after.hurt <= 55, after.hurt
        assert after.hurt < before.hurt, (before.hurt, after.hurt)
        assert "объяснил" in after.hurt_reason, after.hurt_reason
    print("ok  эмоции: объяснение смягчает обиду за молчание")


def test_ignore_accumulates_hurt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
            base_hurt=0,
            base_trust=62,
        )
        store = EmotionStore(Path(tmp) / "emotions.json")
        store.observe_user("привет", cfg.character, now=1000.0)
        store.mark_proactive(text="Соскучилась. Ты тут?", now=1000.0 + 3 * 3600)
        before = store.snapshot()
        assert before.hurt == 0
        ignored = 1000.0 + 3 * 3600 + 100 * 60
        store.tick(cfg.character, now=ignored)
        state = store.snapshot()
        assert state.hurt > 0, state
        assert state.trust < cfg.character.base_trust, state
        assert state.energy < cfg.character.base_energy, state
        assert state.warmth < DEFAULT_WARMTH, state
        assert state.attachment < cfg.character.attachment, state
        assert state.conflict_since > 0 and state.repair_progress < 10
        # ответ пришёл — ожидание снято, но накопленная обида осталась
        store.observe_user("извини, пропал", cfg.character, now=ignored + 300)
        after = store.snapshot()
        assert after.awaiting_since == 0
        assert after.hurt >= state.hurt - 30, (state.hurt, after.hurt)
        if after.hurt > 0:
            assert after.repair_progress > 0
        else:
            assert after.conflict_since == 0 and after.repair_progress == 0
    print("ok  эмоции: игнор копит обиду, а не забывается")


def test_quiet_hours_block_messages() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(
            quiet_hours_start="22:00",
            quiet_hours_end="08:00",
        )
        store = EmotionStore(Path(tmp) / "emotions.json")
        # 23:00 локального времени — внутри тихих часов
        moment = time.mktime((2026, 11, 22, 23, 0, 0, 0, 0, -1))
        store.observe_user("привет", cfg.character, now=moment - 3 * 3600)
        store.tick(cfg.character, now=moment)
        assert not store.should_reach_out(cfg.character, now=moment)
    print("ok  эмоции: тихие часы блокируют ночные сообщения")


def test_advanced_custom_prompt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(
            advanced=True,
            custom_prompt="Говори мягко и с лёгкой иронией, без сюсюканья.",
        )
        store = EmotionStore(Path(tmp) / "emotions.json")
        state = store.snapshot()
        assert "с лёгкой иронией" in prompt_block(cfg, state)
        cfg.character.advanced = False
        assert "с лёгкой иронией" not in prompt_block(cfg, state)
    print("ok  эмоции: расширенное описание работает только в advanced")


def test_warmth_and_attachment_change_with_emotions() -> None:
    cfg = _cfg(attachment=70)
    with tempfile.TemporaryDirectory() as tmp:
        store = EmotionStore(Path(tmp) / "emotions.json")
        state = store.snapshot()
        # теплота живёт только в состоянии, без отдельной базовой черты
        assert state.warmth == DEFAULT_WARMTH
        assert current_attachment(cfg.character, state) == 70

        state.warmth = 15
        state.attachment = 12
        state.hurt = 80
        state.mood = -50
        state.hurt_reason = "пользователь сказал: «ты меня не слышишь»"
        assert state.warmth == 15
        assert current_attachment(cfg.character, state) == 12
        block = prompt_block(cfg, state)
        assert "Обида сильная" in block
        assert "Не прячь" in block
        assert "не пересказывай её снова целиком" in block
        assert "Не извиняйся и не бери вину на себя без реальной причины" in block
        state.conflict_since = 900.0
        state.repair_progress = 45
        block = prompt_block(cfg, state)
        assert "Примирение не обязано быть извинением" in block
        assert "понемногу остываешь" in block
        assert "Теплота сейчас низкая" in block
        assert "Привязанность сейчас приглушена" in block
        assert "теплота 15/100" in block

        state.hurt_reason = ""
        no_reason = prompt_block(cfg, state)
        assert "точная причина не записана" in no_reason
        assert "не придумывай слова" in no_reason
    print("ok  эмоции: теплота и привязанность меняются вместе с обидами")


def test_summary_and_reset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg(base_mood=25, base_trust=70)
        store = EmotionStore(Path(tmp) / "emotions.json")
        store.observe_user("Ты дура", cfg.character, now=1000.0)
        assert "обида" in summary_line(cfg, store.snapshot())
        assert "теплота" in summary_line(cfg, store.snapshot())
        state = store.snapshot()
        text = summary_text(cfg, state)
        assert "Базовые черты" in text and "Настроение" in text
        assert "Сейчас: теплота" in text
        assert "Примирение" in text
        card = summary_html(cfg, state)
        assert "<b>Характер (базовые черты)</b>" in card and "▰" in card
        assert "Сейчас (меняется от общения)" in card
        state.last_user_text = "<b>не формат</b>"
        card = summary_html(cfg, state)
        assert (
            "<blockquote expandable>&lt;b&gt;не формат&lt;/b&gt;</blockquote>"
            in card
        )
        assert "Последняя реплика: «" not in card
        state.last_proactive_text = "Я скучаю. <i>Ответь</i>, когда сможешь."
        card = summary_html(cfg, state)
        assert "💭 <b>Последняя инициатива</b>" in card
        assert (
            "<blockquote expandable>Я скучаю. &lt;i&gt;Ответь&lt;/i&gt;, "
            "когда сможешь.</blockquote>"
            in card
        )
        state = store.reset(cfg.character, now=1100.0)
        assert state.mood == cfg.character.base_mood
        assert state.trust == cfg.character.base_trust
        assert state.warmth == DEFAULT_WARMTH
        assert state.attachment == cfg.character.attachment
        assert prompt_block(cfg, state)
    print("ok  эмоции: /emotions и сброс к базовым чувствам")


def test_reach_out_claim_is_exclusive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = EmotionStore(Path(tmp) / "emotions.json")
        now = time.time()
        assert store.claim_reach_out(now=now)
        assert not store.claim_reach_out(now=now + 1), "второй процесс не должен войти"
        store.release_reach_out_claim()
        assert store.claim_reach_out(now=now + 2)
        store.release_reach_out_claim()
        assert store.claim_reach_out(now=now + 3)
        # замок от упавшего процесса протухает
        assert store.claim_reach_out(now=now + 3 + 181, ttl=180)
        store.release_reach_out_claim()
    print("ok  эмоции: GUI и Telegram делят один замок инициативы")


def test_manager_observes_and_writes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _cfg()
        store = EmotionStore(Path(tmp) / "emotions.json")
        manager = EmotionManager(cfg, store=store, interval=3600)
        manager.observe("Спасибо!")
        assert manager.state().last_user_at > 0
        assert manager.reset().mood == cfg.character.base_mood
    print("ok  эмоции: менеджер обновляет и сбрасывает общее состояние")


def main() -> int:
    test_state_changes_and_persists()
    test_sensitivity_changes_hurt()
    test_proactive_grows_from_loneliness()
    test_followup_has_no_message_cap()
    test_hurt_reason_is_recorded()
    test_explanations_do_not_count_as_insults()
    test_explanation_softens_ignored_hurt()
    test_ignore_accumulates_hurt()
    test_quiet_hours_block_messages()
    test_advanced_custom_prompt()
    test_warmth_and_attachment_change_with_emotions()
    test_summary_and_reset()
    test_reach_out_claim_is_exclusive()
    test_manager_observes_and_writes()
    print("\nEMOTIONS TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
