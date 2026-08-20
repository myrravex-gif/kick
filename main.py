import os
import re
import json
import base64
import logging
import threading
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from flask import Flask
import requests

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# .env 파일 로드 (로컬 환경용, Wispbyte에서는 시스템 환경 변수를 그대로 사용)
load_dotenv()

# ---------------------------------------------------------------------------
# 로깅 설정
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("transfer-bot")

# ---------------------------------------------------------------------------
# 환경 변수 로드 및 검증
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID")
EDUCATION_GUILD_ID = os.environ.get("EDUCATION_GUILD_ID")      # 추방 대상 서버 ID
COMMAND_GUILD_ID = os.environ.get("COMMAND_GUILD_ID")          # 슬래시 명령어 사용 서버 ID
GOOGLE_SERVICE_ACCOUNT = os.environ.get("GOOGLE_SERVICE_ACCOUNT")

ROBLOX_COOKIE = os.environ.get("ROBLOX_COOKIE")
ROBLOX_GROUP_ID = os.environ.get("ROBLOX_GROUP_ID")
TRAINEE_ROLE_ID = os.environ.get("TRAINEE_ROLE_ID")
EDUCATION_ROBLOX_GROUP_ID = os.environ.get("EDUCATION_ROBLOX_GROUP_ID")
ADMIN_ROLE_IDS = os.environ.get("ADMIN_ROLE_IDS")  # 여러 관리자 역할 (쉼표로 구분)

TICKETY_BOT_ID = int(os.environ.get("TICKETY_BOT_ID", 0))

GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "전출/전역 시트")
GOOGLE_WORKSHEET_NAME = os.environ.get("GOOGLE_WORKSHEET_NAME", "Raw Data")


def validate_env_vars():
    required = {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "TARGET_CHANNEL_ID": TARGET_CHANNEL_ID,
        "LOG_CHANNEL_ID": LOG_CHANNEL_ID,
        "EDUCATION_GUILD_ID": EDUCATION_GUILD_ID,
        "COMMAND_GUILD_ID": COMMAND_GUILD_ID,
        "GOOGLE_SERVICE_ACCOUNT": GOOGLE_SERVICE_ACCOUNT,
        "ROBLOX_COOKIE": ROBLOX_COOKIE,
        "ROBLOX_GROUP_ID": ROBLOX_GROUP_ID,
        "TRAINEE_ROLE_ID": TRAINEE_ROLE_ID,
        "EDUCATION_ROBLOX_GROUP_ID": EDUCATION_ROBLOX_GROUP_ID,
        "ADMIN_ROLE_IDS": ADMIN_ROLE_IDS,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise EnvironmentError(
            f"필수 환경 변수가 설정되지 않았습니다: {', '.join(missing)}. "
            "Wispbyte 환경 변수 또는 .env 파일을 확인해주세요."
        )

    try:
        int(TARGET_CHANNEL_ID)
        int(LOG_CHANNEL_ID)
        int(EDUCATION_GUILD_ID)
        int(COMMAND_GUILD_ID)
        int(ROBLOX_GROUP_ID)
        int(TRAINEE_ROLE_ID)
        int(EDUCATION_ROBLOX_GROUP_ID)
        for role_id in ADMIN_ROLE_IDS.split(","):
            int(role_id.strip())
    except ValueError:
        raise EnvironmentError(
            "채널 ID, 서버 ID, 그룹 ID, 역할 ID 값은 모두 숫자여야 합니다."
        )


validate_env_vars()
TARGET_CHANNEL_ID = int(TARGET_CHANNEL_ID)
LOG_CHANNEL_ID = int(LOG_CHANNEL_ID)
EDUCATION_GUILD_ID = int(EDUCATION_GUILD_ID)
COMMAND_GUILD_ID = int(COMMAND_GUILD_ID)
ROBLOX_GROUP_ID = int(ROBLOX_GROUP_ID)
TRAINEE_ROLE_ID = int(TRAINEE_ROLE_ID)
EDUCATION_ROBLOX_GROUP_ID = int(EDUCATION_ROBLOX_GROUP_ID)
ADMIN_ROLE_IDS_LIST = [role_id.strip() for role_id in ADMIN_ROLE_IDS.split(",")]

# ---------------------------------------------------------------------------
# Flask keep_alive 서버
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "봇이 정상적으로 실행 중입니다."


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# 구글 시트 연동
# ---------------------------------------------------------------------------
GOOGLE_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def _load_service_account_info(raw_value: str) -> dict:
    raw_value = raw_value.strip()
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        pass

    try:
        decoded = base64.b64decode(raw_value).decode("utf-8")
        return json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT 값을 JSON 또는 Base64(JSON)로 해석할 수 없습니다."
        ) from exc


def get_worksheet():
    service_account_info = _load_service_account_info(GOOGLE_SERVICE_ACCOUNT)
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(
        service_account_info, GOOGLE_SCOPE
    )
    client_gs = gspread.authorize(credentials)
    spreadsheet = client_gs.open(GOOGLE_SHEET_NAME)
    worksheet = spreadsheet.worksheet(GOOGLE_WORKSHEET_NAME)
    return worksheet


try:
    WORKSHEET = get_worksheet()
    logger.info(
        "구글 시트 연동 성공: %s / %s", GOOGLE_SHEET_NAME, GOOGLE_WORKSHEET_NAME
    )
except Exception as exc:
    logger.error("구글 시트 연동 실패: %s", exc)
    raise

# ---------------------------------------------------------------------------
# 로블록스 API 연동 함수들
# ---------------------------------------------------------------------------
def get_roblox_user_id(username: str) -> int:
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [username], "excludeBannedUsers": True}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            data = response.json().get("data", [])
            if data:
                return data[0].get("id")
    except Exception as exc:
        logger.error("로블록스 유저 ID 조회 중 오류 발생: %s", exc)
    return None


def change_roblox_group_rank(group_id: int, user_id: int, role_id: int) -> bool:
    url = f"https://groups.roblox.com/v1/groups/{group_id}/users/{user_id}"
    cookies = {".ROBLOSECURITY": ROBLOX_COOKIE}

    try:
        session = requests.Session()
        session.cookies.update(cookies)

        resp = session.patch(url, json={"roleId": role_id})
        csrf_token = resp.headers.get("x-csrf-token")

        if not csrf_token:
            logger.error("로블록스 CSRF 토큰을 획득하지 못했습니다.")
            return False

        headers = {"X-CSRF-TOKEN": csrf_token}
        resp = session.patch(url, json={"roleId": role_id}, headers=headers)

        if resp.status_code == 200:
            logger.info("🎉 로블록스 그룹(%s) 랭크 변경 성공: user_id=%s, role_id=%s", group_id, user_id, role_id)
            return True
        elif resp.status_code == 400:
            if "same role" in resp.text:
                logger.info("로블록스 랭크 변경 생략: 유저가 이미 해당 역할(훈련병)을 가지고 있습니다. (user_id=%s)", user_id)
                return True
            logger.warning("로블록스 랭크 변경 실패 (400 Bad Request): 응답: %s", resp.text)
            return False
        elif resp.status_code == 403:
            logger.error("로블록스 랭크 변경 실패 (403 Forbidden): 응답: %s", resp.text)
            return False
        else:
            logger.error("로블록스 그룹 랭크 변경 실패: status=%s, body=%s", resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.exception("로블록스 그룹 랭크 변경 중 예외 발생: %s", exc)
    return False


def exile_roblox_group_user(group_id: int, user_id: int) -> bool:
    url = f"https://groups.roblox.com/v1/groups/{group_id}/users/{user_id}"
    cookies = {".ROBLOSECURITY": ROBLOX_COOKIE}

    try:
        session = requests.Session()
        session.cookies.update(cookies)

        resp = session.delete(url)
        csrf_token = resp.headers.get("x-csrf-token")

        if not csrf_token:
            logger.error("로블록스 CSRF 토큰을 획득하지 못했습니다.")
            return False

        headers = {"X-CSRF-TOKEN": csrf_token}
        resp = session.delete(url, headers=headers)

        if resp.status_code == 200:
            logger.info("로블록스 그룹(%s) 추방 성공: user_id=%s", group_id, user_id)
            return True
        elif resp.status_code in (400, 403, 404):
            logger.info("로블록스 그룹에 이미 존재하지 않음 (그룹 추방 생략): status=%s", resp.status_code)
            return True
        else:
            logger.error("로블록스 그룹 추방 실패: status=%s, body=%s", resp.status_code, resp.text)
            return False
    except Exception as exc:
        logger.exception("로블록스 그룹 추방 중 예외 발생: %s", exc)
    return False


async def handle_roblox_actions(roblox_nickname: str, message_type: str) -> bool:
    roblox_user_id = get_roblox_user_id(roblox_nickname)
    if not roblox_user_id:
        logger.warning("로블록스 닉네임 '%s'에 해당하는 유저 ID를 찾을 수 없습니다.", roblox_nickname)
        return False

    success = True
    if message_type == "전역":
        r1 = change_roblox_group_rank(ROBLOX_GROUP_ID, roblox_user_id, TRAINEE_ROLE_ID)
        r2 = exile_roblox_group_user(EDUCATION_ROBLOX_GROUP_ID, roblox_user_id)
        if not (r1 and r2):
            success = False
    elif message_type == "전출":
        r2 = exile_roblox_group_user(EDUCATION_ROBLOX_GROUP_ID, roblox_user_id)
        if not r2:
            success = False
            
    return success


# ---------------------------------------------------------------------------
# 디스코드 공통 패턴 및 필터
# ---------------------------------------------------------------------------
DISCORD_MENTION_PATTERN = re.compile(r"<[@#][!&]?\d+>")

BLACKLIST_KEYWORDS = [
    "공지",
    "안내",
    "양식",
    "템플릿",
    "규정",
    "주의사항",
]

RELEVANCE_KEYWORDS = [
    "전출",
    "전역",
    "전1출",
    "나갈래",
    "갈래요",
    "그만",
    "접을래",
    "사유",
    "이유",
    "아이디",
    "ID",
    "로블닉",
    "로블록스",
    "닉네임",
    "Ticket Created",
    "created a ticket",
]


def is_blacklisted(content: str) -> bool:
    return any(word in content for word in BLACKLIST_KEYWORDS)


def is_relevant_message(message: discord.Message) -> bool:
    content = message.content or ""
    if any(keyword in content for keyword in RELEVANCE_KEYWORDS):
        return True
    
    for embed in message.embeds:
        embed_text = f"{embed.title or ''} {embed.description or ''}"
        for field in embed.fields:
            embed_text += f" {field.name} {field.value}"
        if any(keyword in embed_text for keyword in RELEVANCE_KEYWORDS):
            return True
            
    return False


# ---------------------------------------------------------------------------
# 유저네임, 숫자 ID, 멘션 변환 및 추출 함수들
# ---------------------------------------------------------------------------
async def resolve_identifier_to_user_id(guild: discord.Guild, identifier: str) -> str:
    if not identifier:
        return identifier
    identifier = identifier.strip()
    
    mention_match = re.search(r"<@\D*(\d+)>", identifier)
    if mention_match:
        return mention_match.group(1)
        
    if identifier.isdigit() and 15 <= len(identifier) <= 21:
        return identifier
        
    if guild:
        lower_id = identifier.lower()
        for member in guild.members:
            if member.name and member.name.lower() == lower_id:
                return str(member.id)
            if member.display_name and member.display_name.lower() == lower_id:
                return str(member.id)
            
    return identifier


USER_ID_LABEL_PATTERN = re.compile(
    r"(?:디스코드\s*사용자\s*ID|사용자\s*ID|아이디|ID|디스코드|유저|Creator\s*ID)\s*[:：]?\s*([^\s\n]+)", re.IGNORECASE
)


def extract_raw_user_identifier(message: discord.Message) -> str:
    content = message.content or ""
    match = USER_ID_LABEL_PATTERN.search(content)
    if match:
        return match.group(1)

    for embed in message.embeds:
        if embed.description:
            mention_match = re.search(r"<@\D*(\d+)>", embed.description)
            if mention_match:
                return mention_match.group(1)
                
        for field in embed.fields:
            target_text = f"{field.name} {field.value}"
            match = USER_ID_LABEL_PATTERN.search(target_text)
            if match:
                return match.group(1)
            mention_match = re.search(r"<@\D*(\d+)>", field.value)
            if mention_match:
                return mention_match.group(1)

    return str(message.author.id)


def detect_type(message: discord.Message) -> str:
    full_text = message.content or ""
    for embed in message.embeds:
        full_text += f" {embed.title or ''} {embed.description or ''}"
        for field in embed.fields:
            full_text += f" {field.name} {field.value}"

    if "전1출" in full_text or "전출" in full_text:
        return "전출"
    if "전역" in full_text:
        return "전역"

    match = re.search(r"(?:전출\s*/?\s*전역|구분|유형)\s*[:：]?\s*(전출|전역|전1출)", full_text)
    if match:
        val = match.group(1)
        if "출" in val: 
            return "전출"
        return "전역"

    cleaned = re.sub(r"전출\s*/?\s*전역", "", full_text)
    normalized = re.sub(r"[\s\W]+", "", cleaned)

    if any(word in normalized for word in ("전역", "그만", "접을래")):
        return "전역"
    return "전출"


ROBLOX_NICK_PATTERN = re.compile(
    r"(?:로블록스\s*닉네임|로블닉|로블록스|닉네임|닉)\s*[:：]?\s*([^\s\n]+)"
)
BRACKET_TAG_PATTERN = re.compile(r"\[.*?\]")


def extract_roblox_nickname(message: discord.Message) -> str:
    full_text = message.content or ""
    for embed in message.embeds:
        full_text += f" {embed.title or ''} {embed.description or ''}"
        for field in embed.fields:
            full_text += f" {field.name} {field.value}"

    match = ROBLOX_NICK_PATTERN.search(full_text)
    if match:
        nickname = match.group(1).strip()
        nickname = DISCORD_MENTION_PATTERN.sub("", nickname).strip()
        nickname = re.sub(r"[,.<>/?;:'\"\[\]{}]", "", nickname).strip()
        if nickname:
            return nickname

    for embed in message.embeds:
        for field in embed.fields:
            if "Creator" in field.name or "Creator" in field.value:
                clean_name = BRACKET_TAG_PATTERN.sub("", field.value).strip()
                clean_name = DISCORD_MENTION_PATTERN.sub("", clean_name).strip()
                if clean_name:
                    return clean_name

    display_name = getattr(message.author, "display_name", str(message.author))
    cleaned = BRACKET_TAG_PATTERN.sub("", display_name).strip()
    return cleaned if cleaned else display_name


REASON_PATTERN = re.compile(r"(?:사유|이유)\s*[:：]?\s*(.+)", re.DOTALL)


def _normalize_whitespace(text: str) -> str:
    text = DISCORD_MENTION_PATTERN.sub("", text)
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return " ".join(lines).strip()


def extract_reason_text(message: discord.Message) -> str:
    full_text = message.content or ""
    for embed in message.embeds:
        full_text += f" {embed.title or ''} {embed.description or ''}"
        for field in embed.fields:
            full_text += f" {field.name} {field.value}"

    match = REASON_PATTERN.search(full_text)
    if match:
        reason = _normalize_whitespace(match.group(1))
        if reason:
            return reason

    return _normalize_whitespace(full_text)


CATEGORY_KEYWORD_RULES = [
    (
        "활동량/시간 부족",
        [
            "바쁨", "바빠", "바쁜", "활동량", "잠수", "폰압", "현생", 
            "시간부족", "접속부족", "시간이없", "여유가없", "못들어", "못함", "시간", "부족",
        ],
    ),
    (
        "흥미 저하",
        [
            "노잼", "질림", "질려", "질리", "질렸", "흥미", "현타", 
            "지겨움", "매너리즘", "지루", "재미없", "의욕상실", "흥미상실", "답답", "지겨", "지겹", "시시", "귀찮", "재미", "지치", "지침", "지쳐", "지칩",
        ],
    ),
    (
        "현생",
        [
            "학업", "공부", "학교", "수험", "과제", "학원", "대학", "수능", "성적", "학기", "방학끝", "중간", "기말", "현실", "현생", "갓생",
        ],
    ),
    (
        "지인 권유",
        [
            "지인", "친구", "권유", "추천", "동행", "꼬득", "지인추천", "친구따라",
        ],
    ),
    (
        "인간관계 부적응",
        [
            "인간관계", "트러블", "갈등", "선배", "후배", "대인관계", 
            "동료갈등", "기수갈등", "꼰대", "불화", "싸움", "시비", "무시", "후임", "선임", "갑질", "차별", "갈굼", "갈구", "갈굽", "갈궈", "갈구어", "비난",
        ],
    ),
    (
        "재시작",
        [
            "재시작", "다시시작", "새로운시작", "새출발", "처음부터", "새로시작", "리셋", "다시", "초심", "차근", "태초", "시작", "새롭게", "새로운", "천천히",
        ],
    ),
    (
        "체험",
        [
            "체험", "맛보기", "찍먹", "호기심", "경험", "해볼", "놀러", "한번해봄", "맛만봄", "여러", "맛", "해보고", "해볼", "도전", "다양한", "열", "궁금", "한번", "한 번", "시도",
        ],
    ),
    (
        "치안 유지",
        [
            "치안", "순찰", "보안", "방위", "경계", "경비", "보호", 
            "지키", "지킴", "지켜", "수호", "경호", "방어",
            "영내", "경비병", "순찰병", "방위병", "치안유지", "치안임무", "경계근무",
            "질서",
            "지명수배", "수배", "체포", "범인", "잡을", "잡으", "사살", "죽임", "죽일", "안전", "클린", "도둑", "무살", "무단", "게이트", "지킬", "지킵", "범죄자", "범죄", "잡기", "잡을", "잡음", "잡는", "잡고", "영창",
        ],
    ),
    (
        "비사령부 희망",
        [
            "비사령부", "사령부탈출", "사령부이탈", "사령부나가", "사령부떠나", "본부탈출", "자유", "민간인", "사령부", "평범", "시민", "일반", "해방",
        ],
    ),
    (
        "타콘텐츠 선호",
        [
            "콘텐츠", "컨텐츠", "타콘", "장르", "놀거리", "즐길거리", 
            "딴거", "다른거", "색다른", "신규콘텐츠", "타부대게임", "훈련", "야전", "점호", "피", "체력", "채력", "스펙", "장비", "능력치", "스탯", "강화", "무기", "방어구", 
            "스펙업", "템맞춤", "레벨업", "렙업", "아이템", "총기", "복장", "헌병", "MP", "mp", "군사경찰", "군경", "다른", "킬로그", "총", "영창", "멋지", "멋져", "멋집", "멋짐", "점호", "경찰", "강해", "제한", "멋있", "멋지", "멋져", "멋집", "멋짐", "옷", "전투", "경력", "할게", "할 게", "저녁점호", "아침점호", "훈련",
        ],
    ),
    (
        "타보직 복귀",
        [
            "타보직", "원보직", "원위치", "예전보직", "보직변경", "보직바꾸", 
            "보직바꿔", "보직돌아", "원래", "전보", "고향", "돌아", "그리", "그립",
        ],
    ),
    (
        "진급 조건 불만",
        [
            "진급", "승급", "승진", "계급장", "진급조건", "진급안됨", 
            "계급안올라", "승진정체", "시험난이도", "시험이어려",
        ],
    ),
    (
        "제도/업무 부적응",
        [
            "제도", "시스템", "규정", "운영방식", "규칙", "부적응", 
            "시스템불만", "제도노답", "스핀", "스폰지", "시험", "적성", "어려", "어렵", "안맞", "민원", "힘들", "힘듬", "힘듦", "가르치", "가르칠", "영어", "적응", "빡세", "빡셈", "빡셉", "복잡", "일", "업무", "익숙",
        ],
    ),
    (
        "휴식",
        [
            "휴직", "휴식", "휴면", "활동 중단", "은퇴", "이탈", "탈퇴", "종료", "중단", "접", "그만", "쉬기", "쉬려", "쉼", "쉽", "쉬고", "쉴", "스트레스",
        ],
    ),
    (
        "레이더 희망",
        [
            "레이더", "레이다", "래이더", "래이다", "탐지", "관측", "찾", 
            "스캔", "수색", "조회", "서치", "레이더병", "탐지병", "관측병", "망원경", "레이딩", "래이딩",
        ],
    ),
    (
        "본부 희망",
        [
            "본부", "본부희망", "본부 가", "본부로", "본부지획", "본부직할", "본부이동", "HQ",
        ],
    ),
    (
        "개인 사정",
        [
            "개인사정", "사생활", "갠사", "일신상", "개인적", "프라이버시", "집안사정", "개인사",
        ],
    ),
    (
        "특전사 희망",
        [
            "특전", "특수부대", "특수 부대", "특공대", "공수부대", 
            "특수전", "특수작전", "정예", "빨간모자", "특전사지원", "707", "항공단", "9공수", "13", "모집", "공고", "신청", "9여단", "특임", "특수", "SDT", "SWC",
        ],
    ),
]

DEFAULT_CATEGORY = "기타"


def classify_reason(reason_text: str) -> str:
    normalized = re.sub(r"\s+", "", reason_text).lower()
    for category, keywords in CATEGORY_KEYWORD_RULES:
        for keyword in keywords:
            normalized_keyword = re.sub(r"\s+", "", keyword).lower()
            if normalized_keyword in normalized:
                return category
    return DEFAULT_CATEGORY


KST = timezone(timedelta(hours=9))


def format_timestamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_iso_week(dt: datetime) -> str:
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-{iso_week:02d}주차"


# ---------------------------------------------------------------------------
# 디스코드 클라이언트 설정 (명령어 사용 서버 = COMMAND_GUILD_ID 동기화)
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        cmd_guild = discord.Object(id=COMMAND_GUILD_ID)
        self.tree.copy_global_to(guild=cmd_guild)
        await self.tree.sync(guild=cmd_guild)
        logger.info("디스코드 슬래시 명령어 지정 서버(COMMAND_GUILD_ID: %s) 즉시 동기화 완료", COMMAND_GUILD_ID)


client = MyClient()


# ---------------------------------------------------------------------------
# 관리자 피드백 입력 Modal 클래스
# ---------------------------------------------------------------------------
class AdminFeedbackModal(discord.ui.Modal):
    def __init__(self, action_type: str, original_message: discord.Message, original_discord_message: discord.Message, warning_reason: str, author_id: str, message_type: str, user_id: str, roblox_nickname: str, reason_text: str, jump_url: str, sent_messages: list):
        super().__init__(title=f"요청 {action_type} 처리")
        self.action_type = action_type
        self.original_message = original_message
        self.original_discord_message = original_discord_message
        self.warning_reason = warning_reason
        self.author_id = author_id
        self.message_type = message_type
        self.user_id = user_id
        self.roblox_nickname = roblox_nickname
        self.reason_text = reason_text
        self.jump_url = jump_url
        self.sent_messages = sent_messages

        self.feedback_input = discord.ui.TextInput(
            label="관리자 피드백",
            style=discord.TextStyle.paragraph,
            placeholder="신청자에게 전달할 피드백이나 처리 사유를 입력해주세요...",
            required=True,
            max_length=1000,
        )
        self.add_item(self.feedback_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        feedback_text = self.feedback_input.value
        is_approved = (self.action_type == "처리 완료")
        status_label = "처리 완료" if is_approved else "기각"
        embed_color = discord.Color.green() if is_approved else discord.Color.red()

        # 승인(처리 완료)일 때만 구글 시트 기록, 디스코드 추방, 로블록스 처리가 실행됨
        if is_approved:
            try:
                category = classify_reason(self.reason_text)
                msg_dt_kst = datetime.now(KST)
                
                row_data = [
                    format_timestamp(msg_dt_kst),
                    format_iso_week(msg_dt_kst),
                    self.message_type,
                    str(self.user_id),
                    self.roblox_nickname,
                    self.reason_text,
                    category,
                ]

                WORKSHEET.append_row(row_data, value_input_option="USER_ENTERED")
                logger.info("구글 시트 기록 완료: type=%s, user_id=%s, category=%s", self.message_type, self.user_id, category)
                
                await asyncio.sleep(1.0)
                await perform_discord_kick_and_roblox_actions(
                    self.original_discord_message, 
                    str(self.user_id), 
                    self.message_type, 
                    self.roblox_nickname, 
                    self.reason_text, 
                    self.author_id
                )
            except Exception as e:
                logger.exception("처리 완료 작업 중 오류 발생: %s", e)

        # 관리자 DM 메시지 수정 내용 구성
        admin_desc = (
            f"**상태:** {status_label} (처리자: {interaction.user.mention})\n\n"
            f"{self.warning_reason}\n\n"
            f"• **작성자:** <@{self.author_id}>\n"
            f"• **유형:** {self.message_type}\n"
            f"• **대상자 ID:** `{self.user_id}`\n"
            f"• **로블닉:** `{self.roblox_nickname}`\n"
            f"• **사유:** {self.reason_text}\n"
            f"• {self.jump_url}\n\n"
            f"**관리자 피드백**\n{feedback_text}"
        )
        updated_embed = discord.Embed(
            title=f"전출·전역 처리 {'완료됨' if is_approved else '기각됨'}",
            description=admin_desc,
            color=embed_color,
            timestamp=datetime.now(timezone.utc)
        )

        # 비활성화된 버튼 뷰 생성
        disabled_view = discord.ui.View()
        disabled_view.add_item(discord.ui.Button(label="처리 완료", style=discord.ButtonStyle.green, disabled=True, custom_id="c_btn"))
        disabled_view.add_item(discord.ui.Button(label="기각", style=discord.ButtonStyle.red, disabled=True, custom_id="r_btn"))

        # 모든 관리자들에게 전송된 DM 메시지 임베드 및 버튼 일괄 업데이트
        for msg in self.sent_messages:
            try:
                await msg.edit(embed=updated_embed, view=disabled_view)
            except Exception as e:
                logger.error("관리자 DM 수정 중 오류 발생: %s", e)

        # 2. 타겟 채널의 원본 메시지 리액션 업데이트
        if self.original_discord_message:
            try:
                if client.user:
                    await self.original_discord_message.remove_reaction("⚠️", client.user)
            except Exception:
                pass
            try:
                react_symbol = "✅" if is_approved else "❌"
                await self.original_discord_message.add_reaction(react_symbol)
            except Exception:
                pass

        # 3. 신청자(작성자)에게 결과 DM 전송 (self.author_id 기준)
        try:
            clean_user_id = re.sub(r"\D", "", str(self.author_id))
            if clean_user_id:
                user_obj = await client.fetch_user(int(clean_user_id))
                if user_obj:
                    user_desc = (
                        "요청하신 전출·전역 신청에 대한 처리 결과가 확정되었습니다.\n\n"
                        f"• **작성자:** <@{self.author_id}>\n"
                        f"• **유형:** {self.message_type}\n"
                        f"• **대상자 ID:** `{self.user_id}`\n"
                        f"• **로블닉:** `{self.roblox_nickname}`\n"
                        f"• **사유:** {self.reason_text}\n"
                        f"• {self.jump_url}\n\n"
                        f"**처리 상태:** {status_label} (처리자: {interaction.user.mention})\n\n"
                        f"**관리자 피드백**\n{feedback_text}"
                    )
                    user_embed = discord.Embed(
                        title="[전출/전역 신청 결과 안내]",
                        description=user_desc,
                        color=embed_color,
                        timestamp=datetime.now(timezone.utc)
                    )

                    await user_obj.send(embed=user_embed)
        except Exception as e:
            logger.warning("신청자(작성자)에게 결과 DM 전송 실패 (DM 차단 등): %s", e)


# ---------------------------------------------------------------------------
# 관리자 경고 알림 View 클래스
# ---------------------------------------------------------------------------
class AdminWarningView(discord.ui.View):
    def __init__(self, warning_reason: str, author_id: str, message_type: str, user_id: str, roblox_nickname: str, reason_text: str, jump_url: str, original_message: discord.Message):
        super().__init__(timeout=None)
        self.warning_reason = warning_reason
        self.author_id = author_id
        self.message_type = message_type
        self.user_id = user_id
        self.roblox_nickname = roblox_nickname
        self.reason_text = reason_text
        self.jump_url = jump_url
        self.original_discord_message = original_message
        self.sent_messages = []
        self.is_completed = False

    @discord.ui.button(label="처리 완료", style=discord.ButtonStyle.green, custom_id="admin_complete_btn")
    async def complete_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_completed:
            await interaction.response.send_message("이미 처리가 완료된 건입니다.", ephemeral=True)
            return
        self.is_completed = True
        modal = AdminFeedbackModal(
            action_type="처리 완료",
            original_message=interaction.message,
            original_discord_message=self.original_discord_message,
            warning_reason=self.warning_reason,
            author_id=self.author_id,
            message_type=self.message_type,
            user_id=self.user_id,
            roblox_nickname=self.roblox_nickname,
            reason_text=self.reason_text,
            jump_url=self.jump_url,
            sent_messages=self.sent_messages
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="기각", style=discord.ButtonStyle.red, custom_id="admin_reject_btn")
    async def reject_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_completed:
            await interaction.response.send_message("이미 처리가 완료된 건입니다.", ephemeral=True)
            return
        self.is_completed = True
        modal = AdminFeedbackModal(
            action_type="기각",
            original_message=interaction.message,
            original_discord_message=self.original_discord_message,
            warning_reason=self.warning_reason,
            author_id=self.author_id,
            message_type=self.message_type,
            user_id=self.user_id,
            roblox_nickname=self.roblox_nickname,
            reason_text=self.reason_text,
            jump_url=self.jump_url,
            sent_messages=self.sent_messages
        )
        await interaction.response.send_modal(modal)


# ---------------------------------------------------------------------------
# 경고 알림 전송 함수
# ---------------------------------------------------------------------------
async def send_admin_warning_alert(message: discord.Message, reason: str, message_type: str, user_id: str, roblox_nickname: str, reason_text: str, author_id: str = None):
    try:
        guild = message.guild
        if not guild:
            target_channel = client.get_channel(TARGET_CHANNEL_ID)
            if target_channel:
                guild = target_channel.guild
        if not guild:
            return

        actual_author_id = author_id if author_id else str(message.author.id)

        view = AdminWarningView(
            warning_reason=reason,
            author_id=actual_author_id,
            message_type=message_type,
            user_id=user_id,
            roblox_nickname=roblox_nickname,
            reason_text=reason_text,
            jump_url=message.jump_url,
            original_message=message
        )

        admin_desc = (
            f"**상태:** 대기 중\n\n"
            f"{reason}\n\n"
            f"• **작성자:** <@{actual_author_id}>\n"
            f"• **유형:** {message_type}\n"
            f"• **대상자 ID:** `{user_id}`\n"
            f"• **로블닉:** `{roblox_nickname}`\n"
            f"• **사유:** {reason_text}\n"
            f"• {message.jump_url}"
        )
        embed = discord.Embed(
            title="전출·전역 처리 경고 발생",
            description=admin_desc,
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )

        notified_members = set()
        for role_id_str in ADMIN_ROLE_IDS_LIST:
            admin_role = guild.get_role(int(role_id_str))
            if not admin_role:
                continue
            for member in admin_role.members:
                if member.bot or member.id in notified_members:
                    continue
                notified_members.add(member.id)
                try:
                    sent_msg = await member.send(embed=embed, view=view)
                    view.sent_messages.append(sent_msg)
                except discord.Forbidden:
                    logger.warning("멤버 %s님에게 DM을 보낼 수 없습니다 (DM 차단 등).", member.name)
                except Exception as e:
                    logger.error("DM 전송 중 오류 발생: %s", e)
    except Exception as exc:
        logger.exception("관리자 경고 알림 전송 중 오류 발생: %s", exc)


async def add_warning_reaction_and_alert(message: discord.Message, reason: str, message_type: str, user_id: str, roblox_nickname: str, reason_text: str, author_id: str = None):
    try:
        await message.add_reaction("⚠️")
    except Exception:
        pass
    await send_admin_warning_alert(message, reason, message_type, user_id, roblox_nickname, reason_text, author_id)


# ---------------------------------------------------------------------------
# 실제 액션 처리 함수 (시트 기록 후 버튼 눌렀을 때만 실행)
# ---------------------------------------------------------------------------
async def perform_discord_kick_and_roblox_actions(message: discord.Message, user_id: str, message_type: str, roblox_nickname: str, reason_text: str, author_id: str = None):
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        logger.error("로그 채널을 찾을 수 없습니다: ID=%s", LOG_CHANNEL_ID)
        return

    ten_hours_ago = datetime.now(timezone.utc) - timedelta(hours=10)
    has_valid_log = False

    try:
        async for log_msg in log_channel.history(limit=200):
            if log_msg.created_at < ten_hours_ago:
                break
            
            is_valid_ticket_log = False
            for embed in log_msg.embeds:
                embed_title = embed.title or ""
                embed_desc = embed.description or ""
                
                if "Ticket Created" in embed_title or "created a ticket" in embed_desc:
                    creator_matched = False
                    
                    if str(user_id) in embed_desc:
                        creator_matched = True
                    
                    for field in embed.fields:
                        field_name = field.name or ""
                        field_value = field.value or ""
                        if "Creator" in field_name or "Creator" in field_value:
                            if str(user_id) in field_value:
                                creator_matched = True
                                break
                    
                    if creator_matched:
                        is_valid_ticket_log = True
                        break
            
            if is_valid_ticket_log:
                has_valid_log = True
                break

    except Exception as exc:
        logger.exception("로그 채널 히스토리 조회 중 오류 발생: %s", exc)
        return

    if not has_valid_log:
        logger.info("10시간 이내 해당 유저(%s)가 직접 생성한(크리에이터) 유효한 티켓 로그 기록이 없어 추방하지 않습니다.", user_id)
        await add_warning_reaction_and_alert(message, "10시간 이내 로그 채널에 해당 유저가 직접 생성한(크리에이터) 유효한 티켓 기록이 없음", message_type, user_id, roblox_nickname, reason_text, author_id)
        return

    guild = client.get_guild(EDUCATION_GUILD_ID)
    if not guild:
        logger.error("디스코드 교육사 서버를 찾을 수 없습니다: ID=%s", EDUCATION_GUILD_ID)
        return

    try:
        member = await guild.fetch_member(int(user_id))
        if member:
            await guild.kick(member, reason="전출/전역 10시간 이내 로그 확인 - 자동 추방 시스템")
            try:
                await message.add_reaction("☑️")
            except Exception:
                pass
            logger.info("디스코드 교육사 서버에서 유저 추방 완료: user_id=%s", user_id)
    except discord.Forbidden:
        logger.error("봇의 디스코드 추방 권한이 부족합니다.")
        await add_warning_reaction_and_alert(message, "봇의 디스코드 서버 추방 권한 부족 (Forbidden)", message_type, user_id, roblox_nickname, reason_text, author_id)
        return
    except discord.NotFound:
        logger.info("디스코드 교육사 서버에 해당 사용자가 이미 존재하지 않습니다: user_id=%s", user_id)
        try:
            await message.add_reaction("☑️")
        except Exception:
            pass
    except Exception as exc:
        logger.exception("디스코드 추방 처리 중 오류 발생: %s", exc)
        await add_warning_reaction_and_alert(message, f"디스코드 추방 처리 중 예외 발생: {exc}", message_type, user_id, roblox_nickname, reason_text, author_id)
        return

    roblox_success = await handle_roblox_actions(roblox_nickname, message_type)
    if not roblox_success:
        await add_warning_reaction_and_alert(message, "로블록스 그룹 랭크 변경 또는 추방 작업 실패", message_type, user_id, roblox_nickname, reason_text, author_id)


async def sync_history_to_sheet(channel):
    logger.info("봇 시작: 시트의 기존 데이터를 보존하며 누락된 히스토리를 동기화합니다...")
    try:
        existing_rows = WORKSHEET.get_all_values()
        headers = ["일시", "주차", "유형", "유저 ID", "로블록스 닉네임", "사유", "분류"]

        if not existing_rows or existing_rows[0] != headers:
            WORKSHEET.update("A1:G1", [headers])
            existing_data_set = set()
        else:
            existing_data_set = {
                (row[0], row[3], row[2]) for row in existing_rows[1:] if len(row) >= 4
            }

        guild = client.get_guild(COMMAND_GUILD_ID) or client.get_guild(EDUCATION_GUILD_ID)

        messages_to_add = []
        async for message in channel.history(limit=None):
            if message.author.bot and (TICKETY_BOT_ID == 0 or message.author.id != TICKETY_BOT_ID):
                continue
            
            if is_blacklisted(message.content or ""):
                continue
            if not is_relevant_message(message):
                continue

            message_type = detect_type(message)
            raw_user_id = extract_raw_user_identifier(message)
            user_id = await resolve_identifier_to_user_id(guild, raw_user_id)
            
            roblox_nickname = extract_roblox_nickname(message)
            reason_text = extract_reason_text(message)
            category = classify_reason(reason_text)

            message_dt_kst = message.created_at.astimezone(KST)
            timestamp_str = format_timestamp(message_dt_kst)
            
            row_key = (timestamp_str, str(user_id), message_type)

            if row_key not in existing_data_set:
                row_data = [
                    timestamp_str,
                    format_iso_week(message_dt_kst),
                    message_type,
                    str(user_id),
                    roblox_nickname,
                    reason_text,
                    category,
                ]
                messages_to_add.append(row_data)

        messages_to_add.reverse()

        if messages_to_add:
            WORKSHEET.append_rows(messages_to_add, value_input_option="USER_ENTERED")
            logger.info("구글 시트 동기화 완료: 누락된 데이터 %d건 추가됨", len(messages_to_add))
        else:
            logger.info("구글 시트 동기화 완료: 추가할 누락된 데이터가 없습니다.")

    except Exception as exc:
        logger.exception("히스토리 동기화 중 오류 발생: %s", exc)


# ---------------------------------------------------------------------------
# 슬래시 명령어 및 이벤트
# ---------------------------------------------------------------------------
@client.tree.command(
    name="추방요청", 
    description="전출/전역 신청을 진행합니다."
)
@app_commands.guilds(discord.Object(id=COMMAND_GUILD_ID))
@app_commands.choices(구분=[
    app_commands.Choice(name="전출", value="전출"),
    app_commands.Choice(name="전역", value="전역")
])
@app_commands.describe(
    구분="전출 또는 전역을 선택하세요.",
    사용자_id="대상자의 디스코드 유저네임, 숫자 ID, 또는 멘션을 입력하세요.",
    로블닉="전출/전역 대상자의 로블록스 닉네임을 입력하세요.",
    사유="요청자가 작성한 신청 사유 메시지를 복사하여 붙여넣으세요."
)
async def chu_bang(
    interaction: discord.Interaction, 
    구분: app_commands.Choice[str], 
    사용자_id: str, 
    로블닉: str, 
    사유: str
):
    await interaction.response.defer(ephemeral=True)

    target_channel = client.get_channel(TARGET_CHANNEL_ID)
    if not target_channel:
        await interaction.followup.send("타겟 채널을 찾을 수 없습니다.", ephemeral=True)
        return

    guild = interaction.guild or client.get_guild(COMMAND_GUILD_ID)
    resolved_user_id = await resolve_identifier_to_user_id(guild, 사용자_id)

    embed = discord.Embed(
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    
    embed.add_field(name="작성자", value=interaction.user.mention, inline=False)
    embed.add_field(name="전출/전역", value=구분.value, inline=False)
    embed.add_field(name="사용자 ID", value=resolved_user_id, inline=False)
    embed.add_field(name="로블닉", value=로블닉, inline=False)
    embed.add_field(name="사유", value=사유, inline=False)

    sent_msg = await target_channel.send(embed=embed)
    
    # 관리자들에게 경고(검토) 알림 전송 (버튼 누르기 전까지 실행되지 않음)
    await send_admin_warning_alert(sent_msg, "슬래시 명령어로 접수된 전출·전역 신청 대기 건", 구분.value, resolved_user_id, 로블닉, 사유, author_id=str(interaction.user.id))

    await interaction.followup.send(f"전출/전역 신청이 정상적으로 접수되었습니다! (인식된 ID: `{resolved_user_id}`)", ephemeral=True)


@client.event
async def on_ready():
    logger.info("디스코드 봇 로그인 완료: %s", client.user)
    channel = client.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        logger.error("지정된 타겟 채널을 찾을 수 없습니다: ID=%s", TARGET_CHANNEL_ID)
        return

    await sync_history_to_sheet(channel)


@client.event
async def on_message(message: discord.Message):
    try:
        if message.author.bot and (TICKETY_BOT_ID == 0 or message.author.id != TICKETY_BOT_ID):
            return

        if message.channel.id != TARGET_CHANNEL_ID:
            return

        if is_blacklisted(message.content or ""):
            return

        if not is_relevant_message(message):
            return

        guild = message.guild or client.get_guild(COMMAND_GUILD_ID)
        
        message_type = detect_type(message)
        raw_user_id = extract_raw_user_identifier(message)
        user_id = await resolve_identifier_to_user_id(guild, raw_user_id)
        
        roblox_nickname = extract_roblox_nickname(message)
        reason_text = extract_reason_text(message)

        author_id = user_id if message.author.bot else str(message.author.id)

        # 메시지 감지 시 바로 처리하지 않고 관리자들에게 검토 대기 알림만 전송
        await send_admin_warning_alert(message, "타겟 채널에서 감지된 전출·전역 신청 대기 건", message_type, user_id, roblox_nickname, reason_text, author_id=author_id)

    except Exception as exc:
        logger.exception("메시지 처리 중 알 수 없는 오류 발생: %s", exc)


def main():
    keep_alive()
    try:
        client.run(DISCORD_TOKEN)
    except Exception as exc:
        logger.exception("디스코드 봇 실행 중 치명적 오류 발생: %s", exc)
        raise


if __name__ == "__main__":
    main()
