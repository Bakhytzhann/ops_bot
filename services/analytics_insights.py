from datetime import date

from services.snapshot_models import zulu

SCHEDULE = "Leon Schedule"
JOURNEY = "Leon Journey Log"
SALES = "Leon Sales"
FORE_FLIGHT = "ForeFlight Performance"
FINANCE = "Счета и выручка компании"


def _coverage(count, total, cohort):
    if total:
        percent = f"{100 * count / total:.1f}".replace(".", ",")
        return f"{count}/{total} ({percent}%) · {cohort}"
    return f"не измерено · {cohort}"


def _decimal(value):
    return f"{value:,.1f}".replace(",", " ").replace(".", ",")


def _hours(value):
    minutes = round(value)
    hours, remainder = divmod(abs(minutes), 60)
    return f"{'−' if minutes < 0 else ''}{hours} ч {remainder:02d} мин"


def _metric(identifier, title, sources, finding, coverage, purpose, missing, action):
    return {
        "id": identifier,
        "title": title,
        "sources": sources,
        "finding": finding,
        "coverage": coverage,
        "purpose": purpose,
        "missing": missing,
        "action": action,
    }


def _compared_time(stats, noncancelled):
    block = stats["block_minutes"]
    delta = stats["block_delta_minutes"]
    facts = []
    if block["count"]:
        facts.append(f"Фактическое блок-время {_hours(block['total'])}")
    if delta["count"]:
        signed_median = f"{delta['median']:+g}".replace(".", ",")
        total_delta = ("+" if delta["total"] > 0 else "") + _hours(delta["total"])
        facts.append(
            f"медиана факт − расписание {signed_median} мин, сумма {total_delta}"
        )
    return _metric(
        "block",
        "Блок-время и отклонение от расписания",
        (SCHEDULE, JOURNEY),
        ". ".join(facts) + "." if facts else "Блок-время за период не получено.",
        _coverage(delta["count"], noncancelled, "пары план–факт среди неотменённых"),
        "Диспетчер уточняет длительность и запас между плечами. Коммерция проверяет часы в смете повторного рейса.",
        "Версия времени в отправленном оффере и ставки затрат за блок-час. Нужна разбивка по направлению и типу ВС.",
        "Ops Manager — найдите направления и типы ВС с повторяющимся отклонением. Передайте коммерции только устойчивые оценки с размером выборки.",
    )


def _fuel_facts(groups):
    return "; ".join(
        f"{_decimal(sample['total'])} {unit} (медиана {_decimal(sample['median'])}, n={sample['count']})"
        for unit, sample in groups.items()
        if sample["count"]
    )


def _fuel(stats):
    actual = stats["actual_count"]
    burn_groups = stats["fuel"]
    uplift_groups = stats["fuel_uplift"]
    burn_count = sum(row["count"] for row in burn_groups.values())
    uplift_count = sum(row["count"] for row in uplift_groups.values())
    burn = _fuel_facts(burn_groups)
    uplift = _fuel_facts(uplift_groups)
    missing_units = stats.get("fuel_missing_unit_count", 0)
    return _metric(
        "fuel",
        "Расход топлива и объём заправки",
        (JOURNEY,),
        f"Расход · {burn or 'нет данных с единицей'}. "
        f"Заправка · {uplift or 'нет данных с единицей'}."
        + (
            f" Расход без единицы указан у {missing_units} рейсов."
            if missing_units
            else ""
        ),
        "Расход "
        + _coverage(burn_count, actual, "рейсы с фактом")
        + ". Заправка "
        + _coverage(uplift_count, actual, "рейсы с фактом"),
        "Экипаж и диспетчер сверяют физический расход. Коммерция уточняет объём в смете по тому же направлению и типу ВС.",
        "Тариф, сборы и оплаченный объём по счёту. Покрытие связки рейс–счёт не измерено.",
        "Финансы — свяжите счёт с рейсом. Сравнивайте оплату заправки и расход по отдельным статьям, чтобы учитывать цену и объём без двойного счёта.",
    )


def _geography(stats):
    actual = stats["actual_count"]
    routes = stats["routes"]
    airports = stats["airports"]
    aircraft = stats["aircraft_types"]
    distance = stats["distance_nm"]
    route_aircraft = stats.get("route_aircraft_count")
    facts = []
    if routes:
        top = "; ".join(
            f"{row['departure']} → {row['arrival']} ({row['count']})"
            for row in routes[:3]
        )
        facts.append(f"Направлений {len(routes)}. Частые направления · {top}")
    if airports:
        top = ", ".join(f"{row['icao']} ({row['count']})" for row in airports[:3])
        facts.append(f"Частые аэропорты · {top}")
    if distance["count"]:
        facts.append(f"Дистанция по журналу {_decimal(distance['total'])} NM")
    coverage = (
        "Направление "
        + _coverage(sum(row["count"] for row in routes), actual, "рейсы с фактом")
        + ". Дистанция "
        + _coverage(distance["count"], actual, "рейсы с фактом")
        + ". Тип ВС "
        + _coverage(sum(row["count"] for row in aircraft), actual, "рейсы с фактом")
    )
    if route_aircraft is not None:
        coverage += ". Маршрут и тип ВС вместе " + _coverage(
            route_aircraft, actual, "рейсы с фактом"
        )
    return _metric(
        "geography",
        "Куда летаем и где есть аналоги",
        (JOURNEY,),
        ". ".join(facts) + "."
        if facts
        else "Фактическая география за период не получена.",
        coverage,
        "Коммерция подбирает аналоги для нового запроса. Ops Manager выбирает станции для сверки цен и качества обслуживания.",
        "Для доходности направления нужны выручка и прямые затраты. Частые маршруты определены по рейсам с известными аэропортами вылета и прилёта.",
        "Коммерция — для расчёта по аналогу проверьте направление, тип ВС и число наблюдений. Ops Manager — начните сверку handling с частых станций.",
    )


def _ferry(stats):
    actual = stats["actual_count"]
    classified = actual - stats["reported_unknown_ferry_count"]
    marked = stats["reported_ferry_count"]
    fact = (
        f"Из {classified} классифицированных выполненных рейсов {marked} помечены перегоном."
        if classified
        else "Долю перегонов среди выполненных рейсов определить пока нельзя."
    )
    return _metric(
        "ferry",
        "Перегоны и возможности их сократить",
        (SCHEDULE, JOURNEY, SALES),
        fact,
        _coverage(classified, actual, "пометка в расписании среди рейсов с фактом"),
        "Коммерция и диспетчер сравнивают, сколько дополнительных плеч требуют разные борта и времена вылета.",
        "Классификация плеч, привязка к заявке, оплата клиентом и фактические затраты каждого перегона.",
        "Ops Manager — проверьте классификацию. Для текущего запроса сравните перегоны по доступным вариантам и внесите разницу в смету.",
    )


def _schedule(stats, noncancelled):
    total = stats["scheduled_count"]
    planned = stats.get("planned_block_minutes", {})
    fact = (
        f"В локальной выборке {total} записей расписания."
        if total
        else "Записей расписания в локальной выборке нет."
    )
    if planned.get("count"):
        fact += f" Плановое блок-время {_hours(planned['total'])}."
    coverage = (
        _coverage(planned["count"], noncancelled, "плановое время неотменённых рейсов")
        if "count" in planned
        else "Покрытие планового блок-времени не измерено."
    )
    return _metric(
        "schedule",
        "Нагрузка по расписанию и свободные окна",
        (SCHEDULE,),
        fact,
        coverage,
        "Диспетчер и Ops Manager видят плановую ротацию. Коммерция получает варианты времени для проверки запроса.",
        "Для доступности нужны подтверждённая позиция, ограничения борта и экипажа, план обслуживания и резервы.",
        "Диспетчер — проверьте соседние рейсы и ограничения, прежде чем передать окно для оффера.",
    )


def build_insights(stats):
    start = date.fromisoformat(stats["start"])
    end = date.fromisoformat(stats["end"])
    days = (end - start).days + 1
    total = stats["scheduled_count"]
    cancelled = stats["cancelled_count"]
    actual = stats["actual_count"]
    noncancelled = total - cancelled
    coverage = _coverage(stats["covered_days"], days, "полностью проверенные дни")
    if stats["last_synced_at"]:
        overview = f"Journey Log · {coverage}. Последняя загрузка {zulu(stats['last_synced_at'])}."
    else:
        overview = f"Journey Log ещё не загружен за выбранный период · {coverage}."
    if total:
        overview += (
            f" В загруженной выборке {total} записей, отменено {cancelled}. "
            f"С фактическими данными {_coverage(actual, noncancelled, 'неотменённые рейсы')}."
        )
    if not stats["complete"]:
        overview += " Метрики ниже относятся к полученной выборке."
    if stats.get("source_failed"):
        overview += (
            " Часть данных Leon не удалось обновить. Использованы доступные записи."
        )
    airborne = stats["airborne_minutes"]
    metrics = [
        _compared_time(stats, noncancelled),
        _fuel(stats),
        _geography(stats),
        _ferry(stats),
        _metric(
            "cancellations",
            "Отмены и их цена",
            (SCHEDULE, JOURNEY, SALES, FINANCE),
            (
                f"Отменено {_coverage(cancelled, total, 'рейсы в загруженной выборке')}."
                if total
                else "В загруженной выборке нет записей расписания."
            ),
            "Покрытие причин и затрат на отмены не измерено.",
            "Менеджмент видит частоту отмен. Ops Manager ищет повторяющиеся причины и проверяет, какие можно устранить.",
            "Подтверждённая причина, результат по заявке и затраты на замену или отмену.",
            "Ops Manager и коммерция — сравните причины у отменённых рейсов и отметьте, удалось ли исполнить заказ другим бортом.",
        ),
        _schedule(stats, noncancelled),
        _metric(
            "performance",
            "Точность расчётов ForeFlight",
            (FORE_FLIGHT, JOURNEY),
            (
                f"Фактическое время в воздухе {_hours(airborne['total'])}, n={airborne['count']}."
                if airborne["count"]
                else "Фактическое время в воздухе за период не получено."
            ),
            "Время в воздухе "
            + _coverage(airborne["count"], actual, "рейсы с фактом")
            + ". Покрытие сопоставленного плана ForeFlight не измерено.",
            "Лётный экипаж и диспетчер проверяют, где расчёт времени и топлива систематически расходится с фактом. Коммерция уточняет бюджет.",
            "Сопоставление рейса, борта и версии расчёта ForeFlight. Для топлива нужны одинаковые единицы и одинаковое определение расхода.",
            "Диспетчер — начните с тех рейсов, где есть сохранённый план. Сравните время в воздухе с ETE, отдельно сверив топливный показатель.",
        ),
        _metric(
            "sales",
            "Конверсия заявок и запас маржи в оффере",
            (SALES, SCHEDULE),
            "Leon Sales даёт запросы и варианты офферов. Данные по всей воронке за период в эту сводку не загружены.",
            "Покрытие заявок, офферов и подтверждённых заказов не измерено.",
            "Коммерция видит, где теряются выполнимые заказы. Менеджмент понимает, повышают ли быстрые ответы и альтернативы число заказов при достаточной марже.",
            "Квалифицированные заявки, время ответа, итог заявки, версии оффера, смета и причина отказа.",
            "Коммерция — свяжите вариант оффера с заявкой и итогом. Сравните время ответа и результаты по одинаковым категориям запросов.",
        ),
        _metric(
            "crew",
            "Дополнительные расходы на экипаж",
            ("Leon Crew / Duty", SALES),
            "Duty и назначения помогают проверить варианты по экипажу. Сводка фактических затрат экипажа за период отсутствует.",
            "Покрытие подтверждений и расходов по вариантам не измерено.",
            "Crew Control подтверждает окно. Коммерция учитывает стоимость подмены, переброски или ночёвки до отправки цены.",
            "Подтверждение Crew Control, привязка экипажа к варианту и стоимость дополнительных ресурсов.",
            "Crew Control — сообщите коммерции, какой вариант подтверждён и какие дополнительные ресурсы потребуются. Финансы уточняют суммы по затратам.",
        ),
        _metric(
            "handling",
            "Услуги и доплаты при исполнении",
            ("Leon Ops / Flight Care", "Ground Status", SALES, FINANCE),
            "Чек-листы и Ground Status позволяют сверить услугу по рейсу. Денежная сверка за период отсутствует.",
            "Покрытие сопоставленных событий, услуг и счетов не измерено.",
            "Наземный супервайзер, кабинный экипаж и коммерция видят, какие услуги заказаны, оказаны и подлежат оплате. Ops Manager видит повторяющиеся доплаты.",
            "Согласованный пакет, факт услуги, счёт подрядчика и договорные условия перевыставления.",
            "Наземный супервайзер и кабина — подтвердите отклонение по услуге. Коммерция проверяет, можно ли по договору включить доплату в счёт.",
        ),
        _metric(
            "margin",
            "Фактическая маржа и разница со сметой",
            (SALES, JOURNEY, FINANCE),
            "Оффер и выполненные плечи можно сопоставить после связывания со счетами. Фактическая маржа за период не рассчитана.",
            "Покрытие рейсов с закрытой выручкой и прямыми затратами не измерено.",
            "Менеджмент узнаёт, чем объясняется разница со сметой, и выбирает, менять ли тариф, поставщика или схему ротации.",
            "Связь оффер–рейс–счёт, оплачиваемые услуги, выручка и прямые расходы в сопоставимой валюте.",
            "Финансы и коммерция — сверяйте смету с итогом по закрытым рейсам и показывайте менеджменту причину самого большого отклонения.",
        ),
    ]
    return {
        "period": f"{start.isoformat()} — {end.isoformat()} (UTC)",
        "overview": overview,
        "metrics": metrics,
    }


def render_insights(report, priorities=()):
    cards = {card["id"]: card for card in report["metrics"]}
    ordered = dict.fromkeys([*(key for key in priorities if key in cards), *cards])
    lines = ["АНАЛИТИКА · ИИ-СВОДКА", report["period"], report["overview"]]
    if priorities:
        lines.append("Сначала показаны метрики, выбранные ИИ.")
    for key in ordered:
        card = cards[key]
        lines.extend(
            [
                "",
                card["title"],
                "Источники · " + " + ".join(card["sources"]),
                "Что известно · " + card["finding"],
                "Покрытие · " + card["coverage"],
                "Зачем · " + card["purpose"],
                "Не хватает · " + card["missing"],
                "Следующий шаг · " + card["action"],
            ]
        )
    return "\n".join(lines)
