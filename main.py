import os
import requests
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from jira import JIRA
from config import GROUPS, KEYWORDS
from dotenv import load_dotenv

# Загружаем переменные из .env файла
load_dotenv()

# Секретные данные из переменных окружения
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
USERNAME = os.getenv("USERNAME")
API_TOKEN = os.getenv("API_TOKEN")

# Создаем структуру папок для логов
log_dir = "AutoJira/logs_autojira"
date_dir = datetime.now().strftime("%Y-%m-%d")
log_path = os.path.join(log_dir, date_dir)
os.makedirs(log_path, exist_ok=True)  # Создаем папку, если её нет

# Пути к логам
auto_assign_log = os.path.join(log_path, f"auto_assign_{datetime.now().strftime('%H-%M-%S')}.log")
auto_assign_success_log = os.path.join(log_path, f"auto_assign_success_{datetime.now().strftime('%H-%M-%S')}.log")

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Основной лог
file_handler = RotatingFileHandler(auto_assign_log, maxBytes=10 * 1024 * 1024, backupCount=5)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)

# Лог успешных операций
success_file_handler = RotatingFileHandler(auto_assign_success_log, maxBytes=10 * 1024 * 1024, backupCount=5)
success_file_handler.setLevel(logging.INFO)
success_file_handler.setFormatter(formatter)

# Настройка логгеров
logger = logging.getLogger("AutoJira")
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)

success_logger = logging.getLogger("successLogger")
success_logger.setLevel(logging.INFO)
success_logger.addHandler(success_file_handler)

# Конфигурация заголовков для запросов
HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Content-Type": "application/json",
}

jira = JIRA(server=JIRA_BASE_URL, token_auth=API_TOKEN)

# Игнорируемые префиксы для задач
IGNORED_PREFIXES = [
    "Заявка на возврат оборудования - Увольнение",
    "Выдача ноутбука -",
    "(Офис)Организация нового рабочего места",
    "Изменение должности/оклада"
]


def get_pending_issues():
    """
    Получает список задач, ожидающих обработки.
    """
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
    """
    Получает количество задач в работе у пользователя.
    """
    jql = f'assignee="{login}" AND status IN ("В работе", "В разработке", "Ожидается ответ пользователя")'
    url = f"{JIRA_BASE_URL}/rest/api/2/search?jql={jql}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    count = response.json()["total"]
    logger.debug(f"Пользователь {login} имеет {count} задач(и) в статусе 'В работе'")
    return count


def assign_issue(issue_key, assignee):
    """
    Назначает задачу на указанного исполнителя.
    """
    jira.assign_issue(issue_key, assignee)
    logger.info(f"Задача {issue_key} назначена на {assignee} (ссылка: {JIRA_BASE_URL}/browse/{issue_key})")


def transition_issue_to_in_progress(issue_key):
    """
    Переводит задачу в статус 'В работе'.
    """
    try:
        transition_id = 21
        jira.transition_issue(issue_key, transition=transition_id)
        logger.info(f"Задача {issue_key} переведена в статус 'В работе'")
    except Exception as e:
        logger.error(f"Ошибка при изменении статуса задачи {issue_key} на 'В работе': {e}")


def distribute_issues():
    """
    Распределяет задачи по группам исполнителей.
    """
    try:
        issues = get_pending_issues()
        for issue in issues:
            summary = issue["fields"]["summary"].lower()
            description = (issue["fields"].get("description") or "").lower()
            issue_key = issue["key"]
            assignee = issue["fields"].get("assignee")

            # Проверка на игнорирование задач с NB-Z*
            if "nb-z" in description:
                if not any(nb_prefix in description for nb_prefix in ["nb-z04", "nb-z4-", "nb-24-"]):
                    logger.info(f"Задача {issue_key} пропущена из-за игнорирования NB-Z* (не NB-Z04* или NB-Z4*).")
                    continue

            # Если исполнитель уже назначен
            if assignee:
                assignee_name = assignee["name"]
                transition_issue_to_in_progress(issue_key)
                assign_issue(issue_key, assignee_name)
                success_logger.info(f"Задача {issue_key} назначена повторно на {assignee_name}")
                continue

            # Пропускаем задачи с игнорируемыми префиксами
            if any(summary.startswith(prefix.lower()) for prefix in IGNORED_PREFIXES):
                logger.info(f"Задача {issue_key} пропущена из-за совпадения с игнорируемыми префиксами")
                continue

            # Определение лучшей группы
            best_group = None
            best_score = 0
            for group, keywords in KEYWORDS.items():
                include_matches = sum(kw in summary or kw in description for kw in keywords["include"])
                exclude_matches = sum(ex_kw in summary or ex_kw in description for ex_kw in keywords["exclude"])

                if include_matches > 0 and exclude_matches == 0:
                    if include_matches > best_score:
                        best_score = include_matches
                        best_group = group

            if best_group:
                assignee = min(GROUPS[best_group], key=get_issue_count_for_user)
                transition_issue_to_in_progress(issue_key)
                assign_issue(issue_key, assignee)
                success_logger.info(f"Задача {issue_key} переведена в 'В работе' и назначена на {assignee}")
            else:
                logger.warning(f"Не удалось определить группу для задачи {issue_key}. Заголовок: {summary}, Описание: {description}")

    except Exception as e:
        logger.error(f"Ошибка при распределении задач: {e}")


if __name__ == "__main__":
    distribute_issues()
