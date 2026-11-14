"""Conservative token decoder. Raw bulletins remain the authoritative output."""

import re

WEATHER = {
    "MI": "тонкий слой",
    "PR": "частичный",
    "BC": "местами",
    "DR": "низовая",
    "BL": "позёмок/перенос ветром",
    "SH": "ливневые",
    "TS": "гроза",
    "FZ": "переохлаждённые",
    "DZ": "морось",
    "RA": "дождь",
    "SN": "снег",
    "SG": "снежные зёрна",
    "IC": "ледяные кристаллы",
    "PL": "ледяная крупа",
    "GR": "град",
    "GS": "мелкий град/снежная крупа",
    "UP": "неопределённые осадки",
    "BR": "дымка",
    "FG": "туман",
    "FU": "дым",
    "VA": "вулканический пепел",
    "DU": "пыль",
    "SA": "песок",
    "HZ": "мгла",
    "PO": "пыльные вихри",
    "SQ": "шквалы",
    "FC": "воронкообразное облако/смерч",
    "SS": "песчаная буря",
    "DS": "пыльная буря",
}


def summarize_metar(raw: str) -> str:
    """Four requested observation fields only; never substitute visibility for RVR."""
    body = re.split(r"\b(?:RMK|TEMPO|BECMG|NOSIG)\b", raw, maxsplit=1)[0]
    tokens = body.rstrip("=").split()
    rvrs, clouds, precipitation = [], [], []
    temperature = None
    unknown_cloud = False
    vertical = None
    for token in tokens:
        rvr = re.fullmatch(
            r"R(\d{2}[LCR]?)/([MP]?\d{4})(?:V([MP]?\d{4}))?(FT)?([UDN])?", token
        )
        if rvr:
            runway, first, last, feet, trend = rvr.groups()

            def value(text):
                return (
                    "<" if text.startswith("M") else ">" if text.startswith("P") else ""
                ) + str(int(text.lstrip("MP")))

            distance = value(first) + ("–" + value(last) if last else "")
            tendency = {"U": " ↑", "D": " ↓", "N": " ="}.get(trend, "")
            rvrs.append(f"ВПП {runway} {distance} {'ft' if feet else 'м'}{tendency}")
        cloud = re.fullmatch(r"(FEW|SCT|BKN|OVC)(\d{3}|///)(?:CB|TCU)?", token)
        if cloud:
            if cloud[2] == "///":
                unknown_cloud = True
            else:
                clouds.append((int(cloud[2]) * 100, cloud[1]))
        if re.fullmatch(r"VV(?:\d{3}|///)", token):
            vertical = (
                "не определена" if token[2:] == "///" else f"{int(token[2:]) * 100} ft"
            )
        temp = re.fullmatch(r"(M?\d{2})/(?:M?\d{2}|XX|//)", token)
        if temp:
            temperature = str(int(temp[1].replace("M", "-"))) + " °C"
        weather = token.lstrip("+-")
        vicinity = weather.startswith("VC")
        if vicinity:
            weather = weather[2:]
        chunks = [weather[i : i + 2] for i in range(0, len(weather), 2)]
        if (
            chunks
            and all(c in WEATHER for c in chunks)
            and any(
                c in {"DZ", "RA", "SN", "SG", "IC", "PL", "GR", "GS", "UP"}
                for c in chunks
            )
        ):
            intensity = (
                "слабые "
                if token.startswith("-")
                else "сильные "
                if token.startswith("+")
                else ""
            )
            precipitation.append(
                intensity
                + " ".join(WEATHER[c] for c in chunks)
                + (" в окрестности" if vicinity else "")
            )
    if clouds:
        height, cover = min(clouds)
        base = f"{height} ft (~{round(height * 0.3048)} м), {cover}"
        if unknown_cloud:
            base += "; есть слой без высоты"
    elif vertical:
        base = f"не определена; VV {vertical}"
    elif unknown_cloud:
        base = "высота не определена"
    elif "CAVOK" in tokens or "NSC" in tokens:
        base = "нет значимой облачности"
    elif any(t in tokens for t in ("SKC", "CLR", "NCD")):
        base = "облака не обнаружены (по сводке)"
    else:
        base = "нет данных"
    rain = ", ".join(precipitation) or (
        "нет значимых (CAVOK)" if "CAVOK" in tokens else "не указаны"
    )
    return (
        f"RVR: {'; '.join(rvrs) or 'не указан'}\nНГО: {base}\n"
        f"Температура: {temperature or 'нет данных'}; осадки: {rain}"
    )
