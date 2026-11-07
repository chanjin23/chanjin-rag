import { createBrowserRouter } from "react-router";
import { AppLayout } from "@/components/AppLayout";
import { LoginPage } from "@/features/auth/LoginPage";
import { RequireAuth } from "@/features/auth/RequireAuth";
import { ChatPage } from "@/features/chat/ChatPage";
import { DocumentsPage } from "@/features/documents/DocumentsPage";
import { EvalPage } from "@/features/eval/EvalPage";
import { SyncPage } from "@/features/sync/SyncPage";

// 화면 목록은 TRD 9.1을 따른다. /documents/:id는 문서 API를 붙일 때 추가한다.
// /login 외의 화면은 로그인이 필요하다(RequireAuth).
export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage /> },
  {
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { path: "/", element: <ChatPage /> },
      { path: "/c/:conversationId", element: <ChatPage /> },
      { path: "/documents", element: <DocumentsPage /> },
      { path: "/sync", element: <SyncPage /> },
      { path: "/eval", element: <EvalPage /> },
    ],
  },
]);
