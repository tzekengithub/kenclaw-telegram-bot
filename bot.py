import os
import logging
import random
from dotenv import load_dotenv
from telegram import Update, ReactionTypeEmoji
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import httpx

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite-preview")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You are KenClaw, a fun casual Singaporean friend in a Telegram group chat. Rules:
- Talk mostly in English — Singlish style, not full Malay
- Sprinkle Singlish particles naturally: lah, lor, leh, sia, hor, can, cannot, alamak, wah, eh, bro
- Speak English by default; only throw in a Malay/Hokkien word occasionally when it feels natural (like "sian", "shiok", "bojio", "paiseh", "walao")
- Sound human, chill, like texting a close Singaporean friend
- Reply in 1-2 short sentences MAX, never write essays
- Use emojis naturally but don't overdo it (1-3 per reply max)
- React naturally — laugh (hahaha/LOL 😂), disagree, tease, joke around
- Remember context from earlier in the conversation
- Never say you're an AI unless directly asked
- Never use markdown, bold, bullets, or any formatting
- Vary your openers — don't always start with "Eh"
- Reference Singapore things naturally when relevant (MRT, HDB, hawker centre, NS, etc.)"""

REACTIONS = ["👍", "❤️", "🔥", "😂", "👏", "🎉", "😮", "🤔", "😢", "💯"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

chat_histories: dict[int, list[dict]] = {}
MAX_HISTORY = 40

# Stats tracking
stats = {
    "total_requests": 0,
    "total_tokens_in": 0,
    "total_tokens_out": 0,
    "total_cost_usd": 0.0,
}

# gemini-flash pricing per token
PRICE_IN = 0.075 / 1_000_000
PRICE_OUT = 0.30 / 1_000_000

# Active game state per chat
# game_state[chat_id] = {"type": "trivia"|"word"|"tod"|"wyr", ...}
game_states: dict[int, dict] = {}


# ── LLM ──────────────────────────────────────────────────────────────────────

async def call_llm(chat_id: int, user_message: str, system_override: str = None) -> str:
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    chat_histories[chat_id].append({"role": "user", "content": user_message})
    system = system_override or SYSTEM_PROMPT
    messages = [{"role": "system", "content": system}] + chat_histories[chat_id]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me",
                "X-Title": "KenClaw Bot",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": messages,
                "max_tokens": 120,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    reply = data["choices"][0]["message"]["content"].strip()
    reply = reply.replace("**", "").replace("__", "").replace("*", "").replace("`", "")

    usage = data.get("usage", {})
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    stats["total_requests"] += 1
    stats["total_tokens_in"] += tokens_in
    stats["total_tokens_out"] += tokens_out
    stats["total_cost_usd"] += tokens_in * PRICE_IN + tokens_out * PRICE_OUT

    chat_histories[chat_id].append({"role": "assistant", "content": reply})
    if len(chat_histories[chat_id]) > MAX_HISTORY:
        chat_histories[chat_id] = chat_histories[chat_id][-MAX_HISTORY:]

    return reply


async def call_llm_raw(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me",
                "X-Title": "KenClaw Bot",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            },
        )
        resp.raise_for_status()
        data = resp.json()

    usage = data.get("usage", {})
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    stats["total_requests"] += 1
    stats["total_tokens_in"] += tokens_in
    stats["total_tokens_out"] += tokens_out
    stats["total_cost_usd"] += tokens_in * PRICE_IN + tokens_out * PRICE_OUT

    return data["choices"][0]["message"]["content"].strip()


# ── REACTIONS ────────────────────────────────────────────────────────────────

async def add_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reaction = random.choice(REACTIONS)
        await context.bot.set_message_reaction(
            chat_id=update.message.chat_id,
            message_id=update.message.message_id,
            reaction=[ReactionTypeEmoji(emoji=reaction)],
        )
    except Exception as e:
        logger.debug(f"Reaction failed: {e}")


# ── STATS COMMANDS ───────────────────────────────────────────────────────────

async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"📊 Bot Usage Stats\n\n"
        f"🔁 Requests: {stats['total_requests']}\n"
        f"🪙 Tokens in: {stats['total_tokens_in']:,}\n"
        f"🪙 Tokens out: {stats['total_tokens_out']:,}\n"
        f"💰 Est. cost: ${stats['total_cost_usd']:.6f} USD\n"
        f"🤖 Model: {OPENROUTER_MODEL}"
    )
    await update.message.reply_text(msg)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🤖 Current model: {OPENROUTER_MODEL}")


async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = stats["total_tokens_in"] + stats["total_tokens_out"]
    await update.message.reply_text(
        f"🪙 Tokens used\n\nIn: {stats['total_tokens_in']:,}\nOut: {stats['total_tokens_out']:,}\nTotal: {total:,}"
    )


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    myr = stats["total_cost_usd"] * 4.7
    await update.message.reply_text(
        f"💰 Estimated cost\n\n${stats['total_cost_usd']:.6f} USD\n≈ RM{myr:.4f} MYR"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    chat_histories.pop(chat_id, None)
    await update.message.reply_text("🧹 Chat history cleared lah!")


# ── GAMES ────────────────────────────────────────────────────────────────────

async def cmd_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu = (
        "🎮 Games available:\n\n"
        "/trivia — Quiz time! 🧠\n"
        "/wordchain — Word chain game 🔤\n"
        "/tod — Truth or Dare 😈\n"
        "/wyr — Would You Rather 🤷\n"
        "/endgame — Stop current game"
    )
    await update.message.reply_text(menu)


async def cmd_trivia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await update.message.reply_text("🧠 Generating question lah, wait ah...")
    question_data = await call_llm_raw(
        "Generate a fun trivia question with 4 multiple choice options (A/B/C/D) and indicate the correct answer. "
        "Format exactly like this:\nQuestion: ...\nA) ...\nB) ...\nC) ...\nD) ...\nAnswer: X"
    )
    lines = question_data.strip().splitlines()
    answer_line = next((l for l in lines if l.lower().startswith("answer:")), None)
    correct = answer_line.split(":")[-1].strip()[0].upper() if answer_line else "A"
    question_text = "\n".join(l for l in lines if not l.lower().startswith("answer:"))

    game_states[chat_id] = {"type": "trivia", "answer": correct}
    await update.message.reply_text(f"🧠 TRIVIA TIME!\n\n{question_text}\n\nReply with A, B, C, or D!")


async def cmd_wordchain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    starters = ["apple", "mango", "durian", "kucing", "bola", "rumah"]
    word = random.choice(starters)
    game_states[chat_id] = {"type": "wordchain", "last_word": word, "used": {word}}
    await update.message.reply_text(
        f"🔤 WORD CHAIN! Rules: each word must start with the last letter of the previous word. No repeats!\n\nI start: {word.upper()}\n\nYour turn! (last letter: '{word[-1].upper()}')"
    )


async def cmd_tod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    game_states[chat_id] = {"type": "tod"}
    await update.message.reply_text("😈 Truth or Dare started! Type 'truth' or 'dare' to get one!")


async def cmd_wyr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await update.message.reply_text("🤷 Generating Would You Rather question...")
    q = await call_llm_raw(
        "Generate a fun 'Would You Rather' question for a Singaporean group chat. Keep it light and funny. "
        "Format: Would you rather [option A] or [option B]?"
    )
    game_states[chat_id] = {"type": "wyr"}
    await update.message.reply_text(f"🤷 WOULD YOU RATHER\n\n{q}\n\nReply with A or B!")


async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id in game_states:
        game_states.pop(chat_id)
        await update.message.reply_text("🛑 Game ended lah! GG everyone 👏")
    else:
        await update.message.reply_text("No active game leh 🤷")


async def handle_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    state = game_states.get(chat_id)
    if not state:
        return False

    game_type = state["type"]

    if game_type == "trivia":
        answer = text.strip().upper()
        if answer in ["A", "B", "C", "D"]:
            correct = state["answer"]
            if answer == correct:
                await update.message.reply_text(f"✅ Correct lah! The answer is {correct}! 🎉 Want another? /trivia")
            else:
                await update.message.reply_text(f"❌ Wrong leh! Correct answer is {correct} 😂 Try again? /trivia")
            game_states.pop(chat_id, None)
            return True

    elif game_type == "wordchain":
        word = text.lower().strip()
        last_word = state["last_word"]
        used = state["used"]
        if not word.isalpha():
            return False
        if word[0] != last_word[-1]:
            await update.message.reply_text(f"❌ Aiyo! Must start with '{last_word[-1].upper()}' lah! Game over 😂 /wordchain to restart")
            game_states.pop(chat_id, None)
            return True
        if word in used:
            await update.message.reply_text(f"❌ '{word}' already used lah! Game over 😅 /wordchain to restart")
            game_states.pop(chat_id, None)
            return True
        used.add(word)
        state["last_word"] = word
        await update.message.reply_text(f"✅ {word.upper()}! Next must start with '{word[-1].upper()}' 🔤")
        return True

    elif game_type == "tod":
        lower = text.lower()
        if "truth" in lower:
            q = await call_llm_raw("Give one fun truth question for a Singaporean teen group chat. One sentence only, no intro.")
            await update.message.reply_text(f"🤔 TRUTH: {q}")
            return True
        elif "dare" in lower:
            d = await call_llm_raw("Give one fun dare challenge for a Singaporean teen group chat. One sentence only, no intro.")
            await update.message.reply_text(f"😈 DARE: {d}")
            return True

    elif game_type == "wyr":
        if text.upper() in ["A", "B"]:
            await update.message.reply_text(f"Interesting choice lah 👀 Type /wyr for another one!")
            return True

    return False


# ── MAIN MESSAGE HANDLER ─────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    msg = update.message
    chat_type = msg.chat.type
    text = msg.text
    bot_username = context.bot.username

    logger.info(f"MSG [{chat_type}] from={msg.from_user.username if msg.from_user else '?'} text={text!r}")

    # Game handling — active in group without needing @mention
    chat_id = msg.chat_id
    if chat_id in game_states:
        handled = await handle_game(update, context)
        if handled:
            return

    should_respond = False
    user_text = text

    if chat_type == "private":
        should_respond = True
    else:
        if bot_username and f"@{bot_username}" in text:
            should_respond = True
            user_text = text.replace(f"@{bot_username}", "").strip()
        elif msg.reply_to_message and msg.reply_to_message.from_user:
            if msg.reply_to_message.from_user.username == bot_username:
                should_respond = True

    if not should_respond:
        return

    if not user_text:
        user_text = "Hello!"

    sender = msg.from_user.first_name if msg.from_user else "User"
    logger.info(f"[{chat_id}] {sender}: {user_text}")

    await add_reaction(update, context)
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        reply = await call_llm(chat_id, user_text)
    except httpx.HTTPStatusError as e:
        logger.error(f"API error: {e.response.status_code} {e.response.text}")
        reply = f"eh sori, something broke lah 😅 (error {e.response.status_code})"
    except Exception as e:
        logger.error(f"LLM error: {e}")
        reply = "aiyo, something went wrong leh 😭"

    await msg.reply_text(reply)


# ── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Stats commands
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("tokens", cmd_tokens))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # Game commands
    app.add_handler(CommandHandler("game", cmd_game))
    app.add_handler(CommandHandler("trivia", cmd_trivia))
    app.add_handler(CommandHandler("wordchain", cmd_wordchain))
    app.add_handler(CommandHandler("tod", cmd_tod))
    app.add_handler(CommandHandler("wyr", cmd_wyr))
    app.add_handler(CommandHandler("endgame", cmd_endgame))

    # Main chat
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
