import re
import os
from datetime import datetime
from tqdm import tqdm
import json


def parse_title(chat_text: str) -> str:
    """
    Parse the title from chat text with multiple fallbacks:
    1. Match a top-level heading starting with "# "
    2. If not found, match a second-level heading "## ..." but not "## SpecStory"
    3. If still not found, match a line starting and ending with ** (Markdown bold)
    4. If nothing matches, return "Untitled"

    Returns the extracted title or "Untitled" if no title is found.
    """
    # 1. Try to match "# Title"
    m = re.search(r"^# (.+)", chat_text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    # 2. Try to match "## Title" but not "## SpecStory"
    m2 = re.search(r"^## (?!SpecStory)(.+)", chat_text, re.MULTILINE)
    if m2:
        return m2.group(1).strip()
    # 3. Try to match bold title lines like **Title (Date)** only on the first line
    m3 = re.search(r"^\*\*(.+?)\*\*$", chat_text.splitlines()[0], re.MULTILINE)
    if m3:
        return m3.group(1).strip()
    # 4. Fallback if no title is found
    return "Untitled"


def extract_timestamp_from_name(name: str):
    """
    Extract timestamp from searches.json "name" field.

    Example input patterns:
    - 2024-09-22_23-03-visualizing-and-fading-a-book.md
    - 2024-09-24_19-26Z-implementing-...md

    Returns timestamp string in "%Y-%m-%d %H:%M:%S" format or None.
    """
    if not name:
        return None

    # 1) First, try to extract YYYY-MM-DD-HH-MM style text, then append seconds.
    m = re.search(r"(\d{4}\D+\d{2}\D+\d{2}\D+\d{2}\D+\d{2})", name)
    if m:
        normalized = re.sub(r"\D+", "-", m.group(1)).strip("-") + "-00"
        try:
            dt = datetime.strptime(normalized, "%Y-%m-%d-%H-%M-%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            print(f"Warning: could not parse timestamp from {name}.")

    # 2) Fallback: extract date-only text and set time to 00:00:00.
    m = re.search(r"(\d{4}\D+\d{2}\D+\d{2})", name)
    if m:
        normalized = re.sub(r"\D+", "-", m.group(1)).strip("-")
        try:
            dt = datetime.strptime(normalized, "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

    print(f"Warning: could not extract any timestamp from {name}.")
    return None


def split_content_blocks(content: str):
    """
    Split a block of content into text/code segments.
    Code blocks are detected using markdown fences (```lang ... ```).

    Returns a list of {"type": lang, "content": text} dictionaries.
    """
    blocks = []
    # Regex to match markdown code blocks: ```lang\n...```
    code_pattern = re.compile(r"```([\w:.\-/]+)\n(.*?)\n```", re.DOTALL)

    def recheck_text_block(text):
        text = text.strip()

        if text.startswith("```"):
            # Handle edge case where text part is also a code block without language
            code_text = text[3:-3].strip()
            return [{"type": "unknown", "content": code_text}]
        elif text.startswith("<think>") or text.startswith("</think>"):
            return [{"type": "think", "content": text}]

        result_blocks = []
        remaining_text = "\n" + text

        # Find all patterns with their positions
        patterns = []

        # Details blocks: \n + any spaces + <details> to </details> or end, or standalone </details>
        for match in re.finditer(
            r"\n\s*<details>.*?(?:</details>|\Z)|\n\s*</details>",
            remaining_text,
            re.DOTALL,
        ):
            patterns.append(
                (match.start(), match.end(), "details", match.group().strip())
            )
        # Tool use blocks: \nTool use: to next \n
        for match in re.finditer(r"\nTool use:[^\n]*", remaining_text, re.DOTALL):
            patterns.append(
                (match.start(), match.end(), "tool-use", match.group().strip())
            )
        # Read file blocks: \nRead file: to next \n
        for match in re.finditer(r"\nRead file:[^\n]*", remaining_text):
            patterns.append(
                (match.start(), match.end(), "read-file", match.group().strip())
            )

        # Sort patterns by position
        patterns.sort(key=lambda x: x[0])

        # Process text sequentially
        last_end = 0
        for start, end, block_type, content in patterns:
            # Skip overlapping patterns
            if start < last_end:
                print("Warning: overlapping patterns detected, skipping.")
                continue
            # Add text before this pattern
            if start > last_end:
                text_before = remaining_text[last_end:start].strip()
                if text_before:
                    result_blocks.append({"type": "text", "content": text_before})
            # Add the pattern block
            result_blocks.append({"type": block_type, "content": content})
            last_end = end

        # Add any remaining text after the last pattern
        if last_end < len(remaining_text):
            text_after = remaining_text[last_end:].strip()
            if text_after:
                result_blocks.append({"type": "text", "content": text_after})

        return result_blocks

    last_end = 0
    for match in code_pattern.finditer(content):
        # 1. Add any text before the current code block
        if match.start() > last_end:
            text_part = content[last_end : match.start()].strip()
            if text_part:
                blocks.extend(recheck_text_block(text_part))
        # 2. Extract language (if provided, e.g., bash/python/diff). Default to "unknown"
        lang = match.group(1) or "unknown"
        code_text = match.group(2).strip()
        # 3. Add the code block with detected language type
        blocks.append({"type": lang.lower(), "content": code_text})
        # Update last_end to the end of the matched code block
        last_end = match.end()

    # 4. Add any remaining text after the last code block
    if last_end < len(content):
        text_part = content[last_end:].strip()
        if text_part:
            blocks.extend(recheck_text_block(text_part))

    return blocks


def parse_chat_roles(chat_text, simple: bool = False):
    """
    Parse chat text into role-message dictionaries.
    Roles can be "User", "Assistant", or "Agent".
    When simple=True, each item is {"role", "content"}.
    When simple=False, each item is {"role", "blocks"} and content is split
    by "\n\n---" and cleaned of leading "_****_".

    Returns a list of dictionaries.
    """
    # This regex matches blocks of chat text with roles (User, Assistant, Agent)
    pattern = re.compile(
        r"_\*\*((?:User|Assistant|Agent)[^\n]*?)\*\*_\n+(.*?)(?=(?:_\*\*(?:User|Assistant|Agent)[^\n]*?\*\*_\n+)|\Z)",
        re.DOTALL,
    )
    results = []
    for match in pattern.finditer(chat_text):
        role = match.group(1)
        content = match.group(2)
        # Normalize role to just "User", "Assistant", or "Agent"
        role = next(
            (r for r in ["User", "Assistant", "Agent"] if role.startswith(r)), "Unknown"
        )

        if simple:
            cleaned_content = re.sub(r"\n\n---\n\n", "\n", content).strip()
            results.append({"role": role, "content": cleaned_content})
            continue

        content_blocks = []
        for block in re.split(r"\n\n---", content):
            block = block.strip()
            if block.startswith("_****_"):
                block = block[len("_****_") :].strip()  # Remove leading "_****_"
            if block:
                content_blocks.append(split_content_blocks(block))
        results.append({"role": role, "blocks": content_blocks})
    return results


os.makedirs("data/parsed_chats", exist_ok=True)
os.makedirs("data/parsed_chats_simple", exist_ok=True)
os.makedirs("data/markdowns_cli", exist_ok=True)

with open("data/searches.json", encoding="utf-8") as f:
    searches = json.load(f)

sha_to_name = {
    item.get("sha"): item.get("name") for item in searches if item.get("sha")
}

for sha in tqdm(sorted(os.listdir("data/markdowns"))):
    with open(f"data/markdowns/{sha}") as f:
        chat_text = f.read()
        # Save the original markdown if it's from CLI agents
        if (
            "\n_**User" not in chat_text
            or "Claude Code Session" in chat_text
            or "WORK SESSION" in chat_text
        ):
            with open(f"data/markdowns_cli/{sha}", "w", encoding="utf-8") as wf:
                wf.write(chat_text)
            continue
        # Parse title and timestamp
        raw_title = parse_title(chat_text)
        title = raw_title
        sha_key = sha[: -len(".md")]
        name = sha_to_name.get(sha_key)
        timestamp = extract_timestamp_from_name(name)
        if timestamp is None:
            print(f"Warning: could not extract timestamp from {name}.")
        # Parse chat roles and content blocks
        msgs = parse_chat_roles(chat_text)
        msgs_simple = parse_chat_roles(chat_text, simple=True)
        # Save parsed chat to JSON
        parsed = {
            "title": title,
            "timestamp": timestamp,
            "platform": "Cursor / Copilot",
            "messages": msgs,
        }
        parsed_simple = {
            "title": title,
            "timestamp": timestamp,
            "platform": "Cursor / Copilot",
            "messages": msgs_simple,
        }
        with open(f"data/parsed_chats/{sha}.json", "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False)
        with open(f"data/parsed_chats_simple/{sha}.json", "w", encoding="utf-8") as f:
            json.dump(parsed_simple, f, indent=2, ensure_ascii=False)

print(f"Total CLI chats: {len(os.listdir('data/markdowns_cli'))}")
print(f"Total parsed chats: {len(os.listdir('data/parsed_chats'))}")
print(f"Total parsed simple chats: {len(os.listdir('data/parsed_chats_simple'))}")
