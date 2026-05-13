import os
import logging
import random
import re
import html
from urllib.parse import quote
from dotenv import load_dotenv
from telegram import BotCommand, Update, ReactionTypeEmoji
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import httpx

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite-preview")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OXFORD_APP_ID = os.getenv("OXFORD_APP_ID")
OXFORD_APP_KEY = os.getenv("OXFORD_APP_KEY")
OXFORD_LANGUAGE = os.getenv("OXFORD_LANGUAGE", "en-gb")
OXFORD_URL = "https://od-api.oxforddictionaries.com/api/v2/entries"
FREE_DICTIONARY_URL = "https://api.dictionaryapi.dev/api/v2/entries/en"

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
dictionary_cache: dict[str, bool] = {}
wiki_context_cache: dict[str, str] = {}

WIKI_SEARCH_LIMIT = 3
WIKI_CONTEXT_CHAR_LIMIT = 4200
WIKI_EXTRACT_CHAR_LIMIT = 900
WIKI_SOURCES = [
    {
        "name": "The Cul de Sac Wiki",
        "api_url": "https://theculdesac.fandom.com/api.php",
        "base_url": "https://theculdesac.fandom.com",
    },
    {
        "name": "Mobile Legends: Bang Bang Wiki",
        "api_url": "https://mobile-legends.fandom.com/api.php",
        "base_url": "https://mobile-legends.fandom.com",
    },
]
WIKI_DOMAIN_TERMS = (
    "cul de sac",
    "cul-de-sac",
    "culdesac",
    "the cul de sac",
    "mlbb",
    "mobile legends",
    "mobile legend",
    "bang bang",
    "hero",
    "heroes",
    "skin",
    "skins",
    "emblem",
    "talent",
    "equipment",
    "battle spell",
    "build",
    "counter",
    "weapon",
    "armor",
    "resource",
    "recipe",
    "drop",
    "obtain",
    "craft",
    "fishing",
    "mining",
    "foraging",
    "catching",
)
WIKI_QUESTION_TERMS = (
    "what",
    "who",
    "where",
    "when",
    "which",
    "how",
    "tell me",
    "explain",
    "guide",
    "best",
    "should i",
)

MENU_TEXT = (
    "KenClaw menu\n\n"
    "Chat:\n"
    "/menu - Show this menu\n"
    "/wiki <topic> - Search Cul de Sac and MLBB wikis\n"
    "/reset - Clear chat memory\n\n"
    "Games:\n"
    "/game - Show games\n"
    "/trivia - Start a trivia question\n"
    "/wordchain - Start the word chain game\n"
    "/tod - Truth or Dare\n"
    "/wyr - Would You Rather\n"
    "/endgame - Stop the current game\n\n"
    "Stats:\n"
    "/usage - Usage summary\n"
    "/tokens - Token usage\n"
    "/cost - Estimated cost\n"
    "/model - Current model"
)

BOT_COMMANDS = [
    BotCommand("start", "Open the bot menu"),
    BotCommand("help", "Show help"),
    BotCommand("menu", "Show commands and features"),
    BotCommand("wiki", "Search the Cul de Sac and MLBB wikis"),
    BotCommand("game", "Show available games"),
    BotCommand("trivia", "Start trivia"),
    BotCommand("wordchain", "Start word chain"),
    BotCommand("tod", "Truth or Dare"),
    BotCommand("wyr", "Would You Rather"),
    BotCommand("endgame", "Stop current game"),
    BotCommand("reset", "Clear chat memory"),
    BotCommand("usage", "Show usage stats"),
    BotCommand("tokens", "Show token usage"),
    BotCommand("cost", "Show estimated cost"),
    BotCommand("model", "Show current model"),
]


# ── LLM ──────────────────────────────────────────────────────────────────────

async def call_llm(
    chat_id: int,
    user_message: str,
    system_override: str = None,
    knowledge_context: str = None,
) -> str:
    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    chat_histories[chat_id].append({"role": "user", "content": user_message})
    system = system_override or SYSTEM_PROMPT
    if knowledge_context:
        system = (
            f"{system}\n\n"
            "Extra wiki knowledge is provided below. Use it when it helps answer the user. "
            "Treat it as more reliable than memory, paraphrase it naturally, and say when the wiki context does not contain enough info. "
            "Keep your normal short casual style.\n\n"
            f"{knowledge_context}"
        )
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


# ── DICTIONARY CHECKING ───────────────────────────────────────────────────────

async def _dictionary_endpoint_has_word(url: str, headers: dict[str, str] = None) -> bool:
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.get(url, headers=headers)

    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False

    resp.raise_for_status()
    return False


async def is_dictionary_word(word: str) -> tuple[bool, bool]:
    cached = dictionary_cache.get(word)
    if cached is not None:
        return cached, True

    if OXFORD_APP_ID and OXFORD_APP_KEY:
        try:
            valid = await _dictionary_endpoint_has_word(
                f"{OXFORD_URL}/{OXFORD_LANGUAGE}/{word.lower()}",
                headers={"app_id": OXFORD_APP_ID, "app_key": OXFORD_APP_KEY},
            )
            dictionary_cache[word] = valid
            return valid, True
        except httpx.HTTPStatusError as e:
            logger.warning(f"Oxford dictionary check failed: {e.response.status_code} {e.response.text}")
        except Exception as e:
            logger.warning(f"Oxford dictionary check failed: {e}")

    try:
        valid = await _dictionary_endpoint_has_word(f"{FREE_DICTIONARY_URL}/{word.lower()}")
        dictionary_cache[word] = valid
        return valid, True
    except Exception as e:
        logger.warning(f"Fallback dictionary check failed: {e}")

    return True, False


# ── WIKI KNOWLEDGE ────────────────────────────────────────────────────────────

def _clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _shorten_text(value: str, limit: int) -> str:
    value = _clean_text(value)
    if len(value) <= limit:
        return value

    shortened = value[: limit - 3].rsplit(" ", 1)[0].strip()
    return f"{shortened}..."


def _extract_wikitext_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
    value = re.sub(r"<ref[^>]*>.*?</ref>", " ", value, flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r"<ref[^/]*/>", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\|-?\|([^=\n]+)=", r"\n\1\n", value)
    value = re.sub(r"\{\{heading\|([^{}]+)\}\}", r"\n\1\n", value, flags=re.IGNORECASE)
    value = re.sub(r"^[^|\n]+\.(?:png|jpg|jpeg|webp|gif)\|([^|\n]+).*$", r"\1", value, flags=re.MULTILINE | re.IGNORECASE)
    value = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", value)
    value = re.sub(r"\{\{[^{}]*\}\}", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"'{2,}", "", value)
    value = re.sub(r"[\[\]{}|=]", " ", value)

    lines = []
    for line in value.splitlines():
        line = _clean_text(line)
        if not line or len(line) <= 1:
            continue
        if line.lower() in {"tabber", "center", "gallery", "br"}:
            continue
        lines.append(line)

    return _clean_text(". ".join(lines))


def _wiki_page_url(source: dict, title: str) -> str:
    page_title = quote(title.replace(" ", "_"), safe="/():'")
    return f"{source['base_url']}/wiki/{page_title}"


def should_lookup_wiki(text: str) -> bool:
    lower = text.lower().strip()
    if len(lower) < 4:
        return False
    if lower in {"hi", "hello", "hey", "yo", "thanks", "thank you", "ok", "okay"}:
        return False
    if any(term in lower for term in WIKI_DOMAIN_TERMS):
        return True
    if "?" in lower and len(lower.split()) >= 3:
        return True
    return any(lower.startswith(term) for term in WIKI_QUESTION_TERMS) and len(lower.split()) >= 3


async def fetch_wiki_pages(source: dict, query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        search_resp = await client.get(
            source["api_url"],
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": WIKI_SEARCH_LIMIT,
                "format": "json",
                "utf8": 1,
            },
        )
        search_resp.raise_for_status()
        search_results = search_resp.json().get("query", {}).get("search", [])

        page_ids = [str(result["pageid"]) for result in search_results if result.get("pageid")]
        if not page_ids:
            return []

        extract_resp = await client.get(
            source["api_url"],
            params={
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "redirects": 1,
                "pageids": "|".join(page_ids),
                "format": "json",
                "utf8": 1,
            },
        )
        extract_resp.raise_for_status()
        pages_by_id = extract_resp.json().get("query", {}).get("pages", {})

        missing_extract_ids = []
        for result in search_results:
            page_id = str(result.get("pageid"))
            page = pages_by_id.get(page_id, {})
            extract = _clean_text(page.get("extract") or result.get("snippet", ""))
            if page_id and not extract:
                missing_extract_ids.append(page_id)

        revision_texts_by_id = {}
        if missing_extract_ids:
            revision_resp = await client.get(
                source["api_url"],
                params={
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "content",
                    "rvslots": "main",
                    "pageids": "|".join(missing_extract_ids),
                    "format": "json",
                    "utf8": 1,
                },
            )
            revision_resp.raise_for_status()
            revision_pages = revision_resp.json().get("query", {}).get("pages", {})
            for page_id, revision_page in revision_pages.items():
                revisions = revision_page.get("revisions") or []
                if not revisions:
                    continue
                slot = revisions[0].get("slots", {}).get("main", {})
                revision_text = slot.get("*") or revisions[0].get("*") or ""
                revision_texts_by_id[page_id] = _extract_wikitext_text(revision_text)

    pages = []
    for result in search_results:
        page_id = str(result.get("pageid"))
        page = pages_by_id.get(page_id, {})
        title = page.get("title") or result.get("title")
        extract = page.get("extract") or result.get("snippet", "")
        if not _clean_text(extract):
            extract = revision_texts_by_id.get(page_id, "")
        if not title or not extract:
            continue

        pages.append(
            {
                "source": source["name"],
                "title": title,
                "url": _wiki_page_url(source, title),
                "extract": _shorten_text(extract, WIKI_EXTRACT_CHAR_LIMIT),
            }
        )

    return pages


async def get_wiki_context(query: str, force: bool = False) -> str:
    query = _clean_text(query)[:180]
    if not query or (not force and not should_lookup_wiki(query)):
        return ""

    cache_key = query.lower()
    cached = wiki_context_cache.get(cache_key)
    if cached is not None:
        return cached

    found_pages = []
    for source in WIKI_SOURCES:
        try:
            found_pages.extend(await fetch_wiki_pages(source, query))
        except Exception as e:
            logger.warning(f"Wiki lookup failed for {source['name']}: {e}")

    if not found_pages:
        wiki_context_cache[cache_key] = ""
        return ""

    lines = ["Wiki context from The Cul de Sac Wiki and Mobile Legends: Bang Bang Wiki:"]
    for page in found_pages[: WIKI_SEARCH_LIMIT * len(WIKI_SOURCES)]:
        lines.append(f"- {page['source']} / {page['title']}: {page['extract']} Source: {page['url']}")

    context = _shorten_text("\n".join(lines), WIKI_CONTEXT_CHAR_LIMIT)
    wiki_context_cache[cache_key] = context
    return context


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


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MENU_TEXT)


async def cmd_wiki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Use /wiki <topic>, like /wiki Layla build or /wiki Elixir.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    wiki_context = await get_wiki_context(query, force=True)
    if not wiki_context:
        await update.message.reply_text("I couldn't find that in the Cul de Sac or MLBB wikis leh.")
        return

    try:
        reply = await call_llm(chat_id, f"Answer this wiki question: {query}", knowledge_context=wiki_context)
    except httpx.HTTPStatusError as e:
        logger.error(f"API error: {e.response.status_code} {e.response.text}")
        reply = f"eh sori, something broke lah 😅 (error {e.response.status_code})"
    except Exception as e:
        logger.error(f"LLM error: {e}")
        reply = "aiyo, something went wrong leh 😭"

    await update.message.reply_text(reply)


# ── GAMES ────────────────────────────────────────────────────────────────────

async def cmd_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    menu = (
        "🎮 Games available:\n\n"
        "/trivia — Quiz time! 🧠\n"
        "/wordchain — Word chain game with dictionary checks 🔤\n"
        "/tod — Truth or Dare 😈\n"
        "/wyr — Would You Rather 🤷\n"
        "/endgame — Stop current game\n"
        "/menu — Show all commands"
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
    starters = ["apple", "mango", "orange", "river", "tiger", "planet"]
    word = random.choice(starters)
    game_states[chat_id] = {"type": "wordchain", "last_word": word, "used": {word}}
    await update.message.reply_text(
        f"🔤 WORD CHAIN! Rules: each English dictionary word must start with the last letter of the previous word. No repeats!\n\nI start: {word.upper()}\n\nYour turn! (last letter: '{word[-1].upper()}')"
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
        if not word.isalpha() or not word.isascii():
            return False
        if word[0] != last_word[-1]:
            await update.message.reply_text(f"❌ Aiyo! Must start with '{last_word[-1].upper()}' lah! Game over 😂 /wordchain to restart")
            game_states.pop(chat_id, None)
            return True
        if word in used:
            await update.message.reply_text(f"❌ '{word}' already used lah! Game over 😅 /wordchain to restart")
            game_states.pop(chat_id, None)
            return True
        is_valid_word, dictionary_checked = await is_dictionary_word(word)
        if not is_valid_word:
            await update.message.reply_text(f"❌ '{word}' not found in the dictionary leh! Game over 😅 /wordchain to restart")
            game_states.pop(chat_id, None)
            return True
        used.add(word)
        state["last_word"] = word
        dictionary_note = "Dictionary check is having issues, so I let this one pass first lah.\n\n" if not dictionary_checked else ""
        await update.message.reply_text(f"{dictionary_note}✅ {word.upper()}! Next must start with '{word[-1].upper()}' 🔤")
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
        wiki_context = await get_wiki_context(user_text)
        reply = await call_llm(chat_id, user_text, knowledge_context=wiki_context)
    except httpx.HTTPStatusError as e:
        logger.error(f"API error: {e.response.status_code} {e.response.text}")
        reply = f"eh sori, something broke lah 😅 (error {e.response.status_code})"
    except Exception as e:
        logger.error(f"LLM error: {e}")
        reply = "aiyo, something went wrong leh 😭"

    await msg.reply_text(reply)


# ── MAIN ─────────────────────────────────────────────────────────────────────

async def setup_bot_commands(application):
    await application.bot.set_my_commands(BOT_COMMANDS)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(setup_bot_commands).build()

    # Stats commands
    app.add_handler(CommandHandler("start", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_menu))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("wiki", cmd_wiki))
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
