import requests
import time
import logging
import logging.config
from jira import JIRA
from config import GROUPS, KEYWORDS
import os
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()

# Секретные данные из переменных окружения
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
USERNAME = os.getenv("USERNAME")
API_TOKEN = os.getenv("API_TOKEN")

# Настройка логирования
logging.config.fileConfig("logging.conf")
logger = logging.getLogger()
success_logger = logging.getLogger("successLogger")

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}

jira = JIRA(server=JIRA_BASE_URL, token_auth=API_TOKEN)

IGNORED_PREFIXES = [
    "Заявка на возврат оборудования - Увольнение",
    "Выдача ноутбука -",
    "(Офис)Организация нового рабочего места",
    "Изменение должности/оклада"
]

def get_pending_issues():
    jql = '''
    project="sd911" 
    AND status="Ожидает обработки" 
    AND "Группа исполнителей" in (TS_MSK_city_team) 
    AND (
        NOT (
            "Customer Request Type" = "Возврат оборудования в ИТ" 
            AND reporter in (srvusr1cv8, srv_1C_ZUP_Jira)
        )
        AND "Customer Request Type" != "(Офис)Организация нового рабочего места"
    ) 
    AND priority != Blocker
    '''
    url = f"{JIRA_BASE_URL}/rest/api/2/search?jql={jql}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    issues = response.json()["issues"]
    logger.info(f"Найдено {len(issues)} задач в статусе 'Ожидает обработки'")
    return issues

def get_issue_count_for_user(login):
    jql = f'assignee="{login}" AND status IN ("В работе", "В разработке", "Ожидается ответ пользователя")'
    url = f"{JIRA_BASE_URL}/rest/api/2/search?jql={jql}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    count = response.json()["total"]
    logger.debug(f"Пользователь {login} имеет {count} задач(и) в статусе 'В работе'")
    return count

def assign_issue(issue_key, assignee):
    jira.assign_issue(issue_key, assignee)
    logger.info(f"Задача {issue_key} назначена на {assignee} (ссылка: {JIRA_BASE_URL}/browse/{issue_key})")

def transition_issue_to_in_progress(issue_key):
    try:
        transition_id = 21
        jira.transition_issue(issue_key, transition=transition_id)
        logger.info(f"Задача {issue_key} переведена в статус 'В работе'")
    except Exception as e:
        logger.error(f"Ошибка при изменении статуса задачи {issue_key} на 'В работе': {e}")

def distribute_issues():
    try:
        issues = get_pending_issues()
        for issue in issues:
            summary = issue["fields"]["summary"].lower()  # Заголовок задачи
            description = (issue["fields"].get("description") or "").lower()  # Описание задачи
            issue_key = issue["key"]  # Идентификатор задачи
            assignee = issue["fields"].get("assignee")  # Текущий исполнитель (если есть)

            # Проверка на игнорирование задач с NB-Z* (но не NB-Z04*)
            if "nb-z" in description:
                if "nb-z04" not in description:
                    logger.info(f"Задача {issue_key} пропущена из-за игнорирования NB-Z* (не NB-Z04*).")
                    continue

            # Если исполнитель уже назначен
            if assignee:
                assignee_name = assignee["name"]
                transition_issue_to_in_progress(issue_key)
                assign_issue(issue_key, assignee_name)
                logger.info(f"Задача {issue_key} уже назначена на {assignee_name}. Переведена в 'В работе'.")
                success_logger.info(f"Задача {issue_key} назначена повторно на {assignee_name}")
                continue

            # Пропускаем задачи с игнорируемыми префиксами
            if any(summary.startswith(prefix.lower()) for prefix in IGNORED_PREFIXES):
                logger.info(f"Задача {issue_key} пропущена из-за совпадения с игнорируемыми префиксами")
                continue

            # Логика определения группы
            best_group = None
            best_score = 0  # Используем для выбора группы с наибольшим количеством совпадений
            for group, keywords in KEYWORDS.items():
                # Подсчет совпадений
                include_matches = sum(kw in summary or kw in description for kw in keywords["include"])
                exclude_matches = sum(ex_kw in summary or ex_kw in description for ex_kw in keywords["exclude"])

                # Если есть включения и нет совпадений с исключениями
                if include_matches > 0 and exclude_matches == 0:
                    logger.debug(f"Задача {issue_key} совпала с ключевыми словами группы {group} (include: {include_matches}).")
                    # Выбираем группу с наибольшим количеством совпадений
                    if include_matches > best_score:
                        best_score = include_matches
                        best_group = group

            # Если группа определена, назначаем задачу
            if best_group:
                assignee = min(GROUPS[best_group], key=get_issue_count_for_user)
                transition_issue_to_in_progress(issue_key)
                assign_issue(issue_key, assignee)
                logger.info(f"Задача {issue_key} переведена в 'В работе' и назначена на {assignee}. Группа: {best_group}")
                success_logger.info(f"Задача {issue_key} переведена в 'В работе' и назначена на {assignee} (ссылка: {JIRA_BASE_URL}/browse/{issue_key})")
                time.sleep(1)  # Задержка для избежания лимитов API
            else:
                # Логируем задачи, которые не удалось распределить
                logger.warning(f"Не удалось определить группу для задачи {issue_key}. Заголовок: {summary}, Описание: {description}")

    except Exception as e:
        logger.error(f"Ошибка при распределении задач: {e}")

distribute_issues()
