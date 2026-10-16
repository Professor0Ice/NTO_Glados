"""Readable phrase rules with Unicode cleanup and explicit word-prefix matching."""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache


logger = logging.getLogger(__name__)
# Compare visual lookalikes in the matcher only; never rewrite original messages.
LOOKALIKES = str.maketrans({
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у", "k": "к", "m": "м", "t": "т", "b": "в", "h": "н",
    "α": "а", "ο": "о", "ρ": "р", "κ": "к", "χ": "х", "ε": "е",
    "і": "и", "ї": "и", "i": "и", "ё": "е",
})


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(char for char in text if unicodedata.category(char) not in {"Cf", "Mn"})
    text = text.translate(LOOKALIKES)

    def clean_word(match):
        word = match.group()
        if any(char.isalpha() for char in word):
            word = word.translate(str.maketrans({"0": "о", "6": "б"}))
            word = re.sub(r"([а-яіїєґ])\1+", r"\1", word)
        return word

    text = re.sub(r"\w+", clean_word, text)
    return " ".join(text.split())


@lru_cache(maxsize=1024)
def compile_pattern(pattern: str) -> re.Pattern:
    normalized = normalize_text(pattern)
    tokens = []
    for token in normalized.split():
        prefix = token.endswith("*")
        word = token[:-1] if prefix else token
        if not word or "*" in word:
            raise ValueError(f"Use '*' only at the end of a word: {pattern!r}")
        # A known keyword may have spaces between its letters: п о д р а б о т к а.
        tokens.append(r"\s*".join(re.escape(char) for char in word)
                      + (r"\w*" if prefix else ""))
    if not tokens:
        raise ValueError("An empty spam pattern is not allowed")
    expression = r"[\s,;:!?.—–-]+".join(tokens)
    if normalized[0].isalnum():
        expression = r"(?<!\w)" + expression
    if normalized[-1].isalnum() or normalized.endswith("*"):
        expression += r"(?!\w)"
    return re.compile(expression)


def find_pattern(text: str, patterns) -> str | None:
    normalized = normalize_text(text)
    return next((pattern for pattern in patterns if compile_pattern(pattern).search(normalized)), None)


@dataclass(frozen=True)
class SpamRule:
    name: str
    groups: tuple[tuple[str, ...], ...]


def load_spam_rules(path="spam_rules.json") -> list[SpamRule]:
    try:
        with open(path, encoding="utf-8-sig") as file:
            entries = json.load(file)
    except FileNotFoundError:
        logger.warning("Файл %s не найден: составные правила антиспама отключены", path)
        return []
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected a list of rules")
    rules = []
    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each rule must be an object")
        name, groups = entry.get("name"), entry.get("all")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"{path}: rule names must be unique, non-empty strings")
        if not isinstance(groups, list) or len(groups) < 2:
            raise ValueError(f"{path}: {name}: at least two groups are required")
        for group in groups:
            if not isinstance(group, list) or not group:
                raise ValueError(f"{path}: {name}: empty or invalid group")
            for pattern in group:
                if not isinstance(pattern, str):
                    raise ValueError(f"{path}: {name}: patterns must be strings")
                compile_pattern(pattern)
        rules.append(SpamRule(name, tuple(tuple(group) for group in groups)))
        names.add(name)
    logger.info("Загружено %s составных правил антиспама из %s", len(rules), path)
    return rules


def find_spam_rule(text: str, rules) -> str | None:
    normalized = normalize_text(text)
    for rule in rules:
        if all(any(compile_pattern(pattern).search(normalized) for pattern in group)
               for group in rule.groups):
            return rule.name
    return None
