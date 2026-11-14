"""Provider 전용 예외 (TRD 6.2 재시도).

외부 API의 오류(openai SDK 예외, httpx 예외, HTTP 상태 코드)는 Provider 밖으로 그대로 내보내지 않고
아래 예외로 바꾼다. 호출하는 쪽(Worker, 질의 그래프)은 이 예외만 보고 처리한다.

| 원인 | 예외 | 재시도 |
| --- | --- | --- |
| 429 | `RateLimited` | 함 |
| 5xx, 타임아웃, 연결 실패 | `ProviderUnavailable` | 함 |
| 401, 403 | `ProviderAuthError` | 안 함 (NVIDIA는 틀린 키에 403 `Authorization failed`를 돌려줌) |
| 402 | `ProviderQuotaExhausted`(`ProviderAuthError`의 하위) | 안 함 (크레딧·사용량 소진) |
| 그 밖의 4xx | `ProviderRequestError` | 안 함 |
| 응답 형식·차원이 다름 | `ProviderResponseError`, `EmbeddingDimensionError` | 안 함 |
| 키·주소 설정 없음 | `ProviderConfigError` | (생성 시) |

예외 메시지에 API 키와 요청·응답 본문을 넣지 않는다.
"""


class ProviderError(Exception):
    """모든 Provider 예외의 부모. `status_code`는 HTTP 응답이 있었을 때만 있다."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderConfigError(ProviderError, ValueError):
    """Provider를 만들 설정(API 키, 주소)이 없거나 잘못됐다."""


class RateLimited(ProviderError):
    """429. 재시도 후에도 계속되면 호출한 쪽이 작업을 미룬다(TRD 6.2: 30분 뒤로 반납)."""


class ProviderUnavailable(ProviderError):
    """5xx, 타임아웃, 연결 실패."""


class ProviderAuthError(ProviderError):
    """401, 403. 키가 틀렸거나 만료됐거나 모델 권한이 없다. 재시도하지 않는다."""


class ProviderQuotaExhausted(ProviderAuthError):
    """402. 크레딧·사용량이 소진됐다. 키 오류처럼 관리자 조치가 필요하므로
    `ProviderAuthError`의 하위로 두어 같은 오류 코드로 다룬다
    (`provider_auth`, TRD 7.3: "API 키 오류, 크레딧 소진")."""


class ProviderRequestError(ProviderError):
    """재시도해도 소용없는 4xx(요청 형식, 모델 이름 오류 등)."""


class ProviderResponseError(ProviderError):
    """응답이 왔지만 형식이 예상과 다르다."""


class EmbeddingDimensionError(ProviderResponseError):
    """임베딩 응답 차원이 `EMBED_DIM`과 다르다(컬럼 차원과 맞지 않아 저장할 수 없음)."""


RETRYABLE: tuple[type[ProviderError], ...] = (RateLimited, ProviderUnavailable)


def error_for_status(status_code: int, what: str) -> ProviderError:
    """HTTP 상태 코드를 Provider 예외로 바꾼다. `what`은 "임베딩" 같은 호출 이름."""
    if status_code in (401, 403):
        return ProviderAuthError(
            f"{what} 인증 실패(HTTP {status_code}). 키가 틀렸거나 만료됐거나 모델 권한이 없습니다",
            status_code=status_code,
        )
    if status_code == 402:
        return ProviderQuotaExhausted(
            f"{what} 크레딧·사용량 소진(HTTP 402). 사용량을 확인하세요", status_code=status_code
        )
    if status_code == 429:
        return RateLimited(f"{what} 호출 한도 초과(HTTP 429)", status_code=status_code)
    if status_code >= 500:
        return ProviderUnavailable(f"{what} 서버 오류(HTTP {status_code})", status_code=status_code)
    return ProviderRequestError(f"{what} 요청 오류(HTTP {status_code})", status_code=status_code)


def provider_error_code(exc: BaseException) -> str:
    """Provider 예외를 오류 코드(TRD 7.3 표의 이름)로 바꾼다. `/health`와 상태 기록에 쓴다.

    크레딧 소진(402, `ProviderQuotaExhausted`)도 키 오류와 같이 `provider_auth`다(관리자 조치 필요).
    둘의 구분은 함께 기록하는 HTTP 상태 코드(`last_status_code`)로 한다.
    """
    if isinstance(exc, RateLimited):
        return "rate_limited"
    if isinstance(exc, ProviderUnavailable):
        return "provider_unavailable"
    if isinstance(exc, ProviderAuthError | ProviderConfigError):
        return "provider_auth"
    if isinstance(exc, ProviderResponseError):
        return "provider_response"
    return "provider_request"
