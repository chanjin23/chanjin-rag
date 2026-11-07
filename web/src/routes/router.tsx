import { createBrowserRouter } from "react-router";
import { AppLayout } from "@/components/AppLayout";
import { ChatPage } from "@/features/chat/ChatPage";
import { DocumentsPage } from "@/features/documents/DocumentsPage";
import { EvalPage } from "@/features/eval/EvalPage";
import { SyncPage } from "@/features/sync/SyncPage";

// 화면 목록은 TRD 9.1을 따른다. /login, /documents/:id는 인증·문서 API를 붙일 때 추가한다.
export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: <ChatPage /> },
      { path: "/c/:conversationId", element: <ChatPage /> },
      { path: "/documents", element: <DocumentsPage /> },
      { path: "/sync", element: <SyncPage /> },
      { path: "/eval", element: <EvalPage /> },
    ],
  },
]);
