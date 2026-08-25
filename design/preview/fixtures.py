"""미리보기용 샘플 컨텍스트.

실제 앱이 이미 넘겨주는 값과, 이번 개편에서 새로 필요한 값을 구분해 둔다.
NEEDS_BACKEND 에 적힌 항목은 아직 orchestrator/web_app 이 만들어 주지 않는다.
"""

NEEDS_BACKEND = {
    "pipeline_slots": "단계별 실행기 점유 현황 (orchestrator 의 executor 상태를 노출해야 함)",
    "job.queue_position": "대기 큐 순번",
    "job.actor": "배우 이름·사진 (web_config 의 NFO actor + .actors/<이름>.jpg)",
    "actor_progress": "배우별 자막 보유율 집계",
    "gpu.utilization_history": "최근 5분 사용률 시계열 (Prometheus range query)",
    "job.stop_reason": "중단 사유 구분 — 지금은 blocked 하나에 사용자 중단·오류·"
                       "서비스 재시작이 섞이고 error 문자열로만 갈린다",
}

# 지어내지 않는다:
#  - 남은 시간: 단계별·청크별 소요를 기록하지 않아 신뢰할 추정 불가. 경과 시간만 쓴다.
#  - GPU 전력/온도 한계: gpu_monitoring 이 쿼리하지 않는다 (POWER_MGMT_LIMIT, SLOWDOWN_TEMP 미수집).
#  - "하이브리드" 라는 모델: WhisperX 1차 + Kotoba 재디코딩 교체 구조라 그런 모델은 없다.

# 2026-08-25 운영 중인 stt.blackark.xyz 에서 직접 확인한 실제 미디어 트리다.
# 개수는 눈으로 센 것만 적는다 — 확인하지 못한 칸은 None 으로 둔다.
#   /media
#     ├ AV/japan          배우 디렉터리 449개 → 그 아래 타이틀 폴더 → 파일
#     ├ AV/west, AV/unclassified
#     ├ Drama, ETC, Movie, Sports
#     └ Variety           타이틀 폴더 3개 + 폴더 없이 놓인 파일 12개
# 카테고리마다 깊이가 다르다는 것이 핵심이다. 균일한 트리로 가정하면 안 된다.
MEDIA_TREE = [
    {"path": "AV/japan", "shape": "actor", "dirs": 449,
     "note": "배우 → 타이틀 → 파일. 미디어 루트에서 가장 큰 구역이다."},
    {"path": "AV/west", "shape": "actor", "dirs": None},
    {"path": "AV/unclassified", "shape": "flat", "dirs": None},
    {"path": "Drama", "shape": "title", "dirs": None},
    {"path": "ETC", "shape": "title", "dirs": None},
    {"path": "Movie", "shape": "title", "dirs": None},
    {"path": "Sports", "shape": "title", "dirs": None},
    {"path": "Variety", "shape": "mixed", "dirs": 3, "files": 12,
     "note": "타이틀 폴더와 낱개 파일이 같은 자리에 섞여 있다."},
]

REMOTE_SERVERS = {
    "configured": True,
    "stt_base_url": "http://192.168.1.20:8100",
    "lm_base_url": "http://192.168.1.30:1234/v1",
    "lm_model": "qwen3-30b-a3b-instruct",
}

# 2026-08-25 운영 실측: 진행 0 · 중단 66 · 실패 0 · 대기 3 · 완료 247.
# 멈춤이 압도적이라는 것이 이 시스템의 실제 모습이다 — 화면에서 격리 구역이 가장 커야 한다.
JOB_STATS = {"running": 3, "waiting": 12, "blocked": 63, "failed": 3, "completed": 247}
STOPPED_REST = 63   # 상세로 보여주는 3건 말고 나머지


def _stages(*specs):
    """(key, label, state, completed, total, percent) 튜플을 단계 목록으로."""
    out = []
    for key, label, state, completed, total, percent in specs:
        out.append(
            {
                "key": key,
                "label": label,
                "state": state,
                "completed": completed,
                "total": total,
                "total_is_estimate": key == "transcription" and state == "running",
                "percent": percent,
            }
        )
    return out


RUNNING_JOBS = [
    {
        "id": "7f21ac03-4b9e-4d21-9c60-2a8f11e0b7d3",
        "source_rel": "AV/japan/사쿠라이 미유/SMP-014 밤의 인터뷰/SMP-014 밤의 인터뷰.mkv",
        "actor": {"name": "사쿠라이 미유", "image": "a"},
        "backend_label": "WhisperJAV",
        "prompt_label": "JAV",
        "status_group": "running",
        "percent": 62,
        "elapsed_seconds": 2148,
        "stages": _stages(
            ("audio", "추출", "done", 0, 0, 100),
            ("transcription", "전사", "done", 240, 240, 100),
            ("translation", "번역", "running", 148, 240, 62),
            ("render", "자막", "pending", 0, 0, 0),
        ),
    },
    {
        "id": "5d19be40-1c22-4a0e-bb31-77c0d2e19a55",
        "source_rel": "AV/japan/하야시 노아/KRD-077 조용한 방/KRD-077 조용한 방.mp4",
        "actor": {"name": "하야시 노아", "image": "b"},
        "backend_label": "하이브리드",
        "prompt_label": "JAV",
        "status_group": "running",
        "percent": 34,
        "elapsed_seconds": 1129,
        "stages": _stages(
            ("audio", "추출", "done", 0, 0, 100),
            ("transcription", "전사", "running", 121, 355, 34),
            ("translation", "번역", "pending", 0, 0, 0),
            ("render", "자막", "pending", 0, 0, 0),
        ),
    },
    {
        "id": "0b31aa77-5e10-4f92-9c00-1d5b9a3e77c1",
        "source_rel": "AV/japan/모리 유이/NKT-660 비 오는 날/NKT-660 비 오는 날.mkv",
        "actor": None,
        "backend_label": "Kotoba",
        "prompt_label": "버라이어티",
        "status_group": "running",
        "percent": 18,
        "elapsed_seconds": 194,
        "stages": _stages(
            ("audio", "추출", "running", 0, 0, 71),
            ("transcription", "전사", "pending", 0, 0, 0),
            ("translation", "번역", "pending", 0, 0, 0),
            ("render", "자막", "pending", 0, 0, 0),
        ),
    },
]

STOPPED_JOBS = [
    {
        "id": "a44e91b2-3f77-4c10-8e21-9b0c4d55e300",
        "source_rel": "AV/japan/아오키 리코/TKR-108 늦은 오후/TKR-108 늦은 오후.mkv",
        "status": "failed",
        "status_label": "실패",
        "stage_label": "전사 92/≈195",
        "changed_at": "13:47:30",
        "reason": "CUDA out of memory (chunk 92)",
        "resume": False,
    },
    {
        "id": "c0731d55-88a1-4e60-a2f0-6d3e1b9a0f42",
        "source_rel": "AV/japan/사쿠라이 미유/MSK-402 흐린 날/MSK-402 흐린 날 [파트 3].mkv",
        "status": "failed",
        "status_label": "실패",
        "stage_label": "전사 33/≈287",
        "changed_at": "12:38:04",
        "reason": "전사 서버 응답 없음 (504)",
        "resume": False,
    },
    {
        "id": "3e90cc71-9d02-4b71-90a3-5c11e7f2b8aa",
        "source_rel": "variety/HRB-519 첫 촬영.mp4",
        "status": "blocked",
        "status_label": "멈춤",
        "stage_label": "번역 88/210",
        "changed_at": "11:02:19",
        "reason": "서비스 재시작 중 끊김",
        "resume": True,
    },
]

COMPLETED_JOBS = [
    {
        "source_rel": "AV/japan/사쿠라이 미유/MSK-402 흐린 날/MSK-402 흐린 날.mkv",
        "meta": "사쿠라이 미유 · WhisperJAV · JAV",
        "transcription": "21분 04초",
        "translation": "44분 18초",
        "total": "1:12:33",
    },
    {
        "source_rel": "variety/버라이어티 2026-08-19.mp4",
        "meta": "Kotoba · 버라이어티",
        "transcription": "17분 51초",
        "translation": "36분 02초",
        "total": "58:14",
    },
    {
        "source_rel": "variety/DMO-233 여름의 기록/DMO-233 여름의 기록 [파트 1].mp4",
        "meta": "하야시 노아 · 하이브리드 · JAV",
        "transcription": "28분 40초",
        "translation": "51분 09초",
        "total": "1:22:47",
    },
]

PIPELINE_SLOTS = [
    {"stage": "추출", "capacity": 1, "job": "NKT-660 비 오는 날.mkv", "detail": "ffmpeg · 스트림 0", "percent": 71},
    {"stage": "전사", "capacity": 1, "job": "KRD-077 조용한 방.mp4", "detail": "하이브리드 · 121 / ≈355", "percent": 34},
    {"stage": "번역", "capacity": 1, "job": "SMP-014 밤의 인터뷰.mkv", "detail": "JAV · 148 / 240", "percent": 62},
]

QUEUE = [
    {"position": 1, "title": "DMO-233 여름의 기록 [파트 2]", "stage": "번역"},
    {"position": 2, "title": "HRB-520 두 번째 촬영", "stage": "전사"},
    {"position": 3, "title": "버라이어티 2026-08-21", "stage": "추출"},
    {"position": 4, "title": "SMP-015 아침의 대화", "stage": "추출"},
]
QUEUE_REST = 8

QUEUE_ETA = "약 4:20:00"

GPU = {
    "available": True,
    "index": 0,
    "display_name": "RTX 3080 10GB",
    "utilization_percent": 94,
    "utilization_history": [62, 71, 68, 84, 91, 88, 95, 93, 96, 90, 94, 94],
    "utilization_min": 62,
    "utilization_avg": 87,
    "memory_percent": 81,
    "memory_used_gib": 8.1,
    "memory_total_gib": 10.0,
    "temperature_celsius": 72,
    "power_watts": 281,
    # hybrid 는 WhisperX 1차 + Kotoba 재디코딩이다. 실제로 메모리에 올라오는 건 이 둘.
    "loaded": [
        {"name": "WhisperX", "state": "디코딩 중", "active": True},
        {"name": "Kotoba", "state": "대기 · 유휴 4분", "active": False},
    ],
}

ACTOR_PROGRESS = [
    {"name": "사쿠라이 미유", "image": "a", "done": 8, "total": 14, "seg": (57, 14, 22, 7)},
    {"name": "아오키 리코", "image": "c", "done": 2, "total": 6, "seg": (33, 17, 50, 0)},
    {"name": "하야시 노아", "image": "b", "done": 7, "total": 9, "seg": (78, 11, 11, 0)},
    {"name": "모리 유이", "image": "d", "done": 4, "total": 4, "seg": (100, 0, 0, 0)},
    # variety 아래에는 배우 디렉터리가 없다 (NFO actor 도 대개 비어 있다).
    {"name": None, "image": None, "done": 26, "total": 31, "seg": (84, 0, 16, 0)},
]

CONTEXT = {
    "remote_servers": REMOTE_SERVERS,
    "job_stats": JOB_STATS,
    "running_jobs": RUNNING_JOBS,
    "stopped_jobs": STOPPED_JOBS,
    "completed_jobs": COMPLETED_JOBS,
    "pipeline_slots": PIPELINE_SLOTS,
    "queue": QUEUE,
    "queue_rest": QUEUE_REST,
    "stopped_rest": STOPPED_REST,
    "media_tree": MEDIA_TREE,
    "queue_eta": QUEUE_ETA,
    "gpu": GPU,
    "actor_progress": ACTOR_PROGRESS,
    "updated_at": "14:22:07",
    "active_nav": "dashboard",
    "jobs_badge": 29,
}
