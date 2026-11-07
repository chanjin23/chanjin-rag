/**
 * 보내지 못한 질문 보존 (TRD 9.2 입력 보존).
 * 401(세션 만료)이나 네트워크 오류로 못 보낸 질문을 대화별로 sessionStorage에 두고,
 * 그 대화를 다시 열면 입력창에 채운다. 탭을 닫으면 사라진다.
 */
const key = (conversationId: string) => `chat-draft:${conversationId}`;

export function saveDraft(conversationId: string, text: string): void {
  try {
    sessionStorage.setItem(key(conversationId), text);
  } catch {
    // 저장소를 쓸 수 없는 환경(사생활 보호 모드 등)에서는 보존하지 않는다
  }
}

export function loadDraft(conversationId: string): string {
  try {
    return sessionStorage.getItem(key(conversationId)) ?? "";
  } catch {
    return "";
  }
}

export function clearDraft(conversationId: string): void {
  try {
    sessionStorage.removeItem(key(conversationId));
  } catch {
    // 무시
  }
}
