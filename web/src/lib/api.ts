/**
 * API client and hooks for TelePlay backend.
 */
import axios from "axios";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";

// Types
export interface User {
  id: number;
  telegram_id: number;
  username: string | null;
  first_name: string | null;
  last_name: string | null;
  created_at: string;
  last_active: string;
}

export interface Folder {
  id: number;
  name: string;
  parent_id: number | null;
  user_id: number;
  created_at: string;
  updated_at: string;
  file_count: number;
  children?: Folder[];
}

export interface TelegramFile {
  id: number;
  user_id: number;
  folder_id: number | null;
  file_id: string;
  file_unique_id: string;
  file_name: string;
  file_size: number;
  mime_type: string | null;
  file_type: "video" | "audio" | "document" | "image";
  duration: number | null;
  width: number | null;
  height: number | null;
  created_at: string;
  updated_at: string;
  stream_url: string;
  fallback_stream_url?: string | null;
  thumbnail_url: string | null;
  last_pos?: number;
  public_hash?: string;
  public_stream_url?: string;
  deleted_at?: string | null;
  purge_after?: string | null;
}

export interface FileListResponse {
  files: TelegramFile[];
  total: number;
  page: number;
  per_page: number;
}

export interface BotInfo {
  username: string;
  name?: string;
  server_version: string;
}

export interface LoginCodeResponse {
  code: string;
  expires_at: string;
}

export interface PollCodeResponse {
  status: "pending" | "claimed";
  message?: string;
  access_token?: string;
  refresh_token?: string;
  user?: User;
}

export interface AuthSession {
  session_id: string;
  current: boolean;
  session_type: "persistent" | "temporary";
  user_agent: string | null;
  created_at: string;
  last_used_at: string;
  last_seen_at: string;
  expires_at: string;
}

export interface StorageStats {
  total_size: number;
  limit: number;
}

export interface StorageAnalytics {
  active: { count: number; size: number };
  trash: { count: number; size: number };
  folders: number;
  by_type: Array<{ type: string; count: number; size: number }>;
  largest_files: Array<{
    id: number;
    file_name: string;
    file_type: string;
    file_size: number;
    folder_id: number | null;
    created_at: string;
    thumbnail_url: string | null;
  }>;
  daily_activity: Array<{ date: string; count: number; size: number }>;
  limit: number;
}

export interface TrashFolder {
  id: number;
  name: string;
  parent_id: number | null;
  deleted_at: string;
  purge_after: string;
  item_count: number;
}

export interface TrashListResponse {
  files: TelegramFile[];
  folders: TrashFolder[];
  total: number;
}

export interface TrashBreadcrumb {
  id: number;
  name: string;
}

export interface TrashBrowseResponse extends TrashListResponse {
  current_folder: TrashFolder;
  breadcrumbs: TrashBreadcrumb[];
}

export interface RecycleBinSettings {
  enabled: boolean;
  retention_days: number;
  updated_items?: number;
}

export interface TrashSelection {
  file_ids: number[];
  folder_ids: number[];
}

export interface AuthResponse {
  access_token: string;
  refresh_token: string;
  user: User;
}

export interface PasswordLoginRequest {
  username: string;
  password: string;
}

export interface UsernameCheckResponse {
  username: string;
  available: boolean;
  valid: boolean;
  reason?: string;
}

export interface WebCredentialProfile {
  username: string | null;
  has_password: boolean;
}

export interface PasswordChangeRequest {
  currentPassword: string;
  newPassword: string;
}

// API client
// VITE_API_BASE_URL is optional. If it is missing, the app uses same-origin /api
// so Vite/nginx/backend proxy config can route requests without exposing a real URL in code.
const trimTrailingSlashes = (value: string): string =>
  value.replace(/\/+$/, "");
const stripApiSuffix = (value: string): string =>
  trimTrailingSlashes(value).replace(/\/api$/, "");

const configuredApiBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
export const API_ORIGIN = configuredApiBaseUrl
  ? stripApiSuffix(configuredApiBaseUrl)
  : "";
export const API_BASE_URL = API_ORIGIN ? `${API_ORIGIN}/api` : "/api";

export const isCredentialedCrossOriginApi = (() => {
  if (!API_ORIGIN || typeof window === "undefined") return false;
  try {
    return new URL(API_ORIGIN, window.location.origin).origin !== window.location.origin;
  } catch {
    return false;
  }
})();

export const mediaCrossOrigin = isCredentialedCrossOriginApi ? "use-credentials" : undefined;

export const SESSION_HINT_KEY = "tp_session_hint";

export const setSessionHint = () => {
  try {
    localStorage.setItem(SESSION_HINT_KEY, "1");
  } catch {
    // Ignore storage access errors. Auth cookies remain the source of truth.
  }
};

export const clearSessionHint = () => {
  try {
    localStorage.removeItem(SESSION_HINT_KEY);
  } catch {
    // Ignore storage access errors.
  }
};

export const hasSessionHint = (): boolean => {
  try {
    return localStorage.getItem(SESSION_HINT_KEY) === "1";
  } catch {
    return false;
  }
};

// Remove old localStorage JWTs from previous builds. Web auth now uses
// HttpOnly cookies so JavaScript cannot read or leak session tokens.
try {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
  localStorage.removeItem("user");
} catch {
  // Ignore storage access errors in private/locked-down browsers.
}

export const toApiUrl = (url: string): string => {
  if (!url) return "";
  if (url.startsWith("http://") || url.startsWith("https://")) return url;
  if (url.startsWith("/api")) {
    return API_ORIGIN ? `${API_ORIGIN}${url}` : url;
  }
  return `${API_BASE_URL}${url.startsWith("/") ? url : `/${url}`}`;
};

export const api = axios.create({
  baseURL: API_BASE_URL,
  withCredentials: true,
});

const UNSAFE_METHODS = new Set(["post", "put", "patch", "delete"]);

// Cookie-auth state-changing requests include a non-simple header. Combined
// with backend Origin/CORS checks, this blocks simple CSRF form attacks while
// keeping Android/TV bearer-token clients compatible.
api.interceptors.request.use((config) => {
  const method = (config.method || "get").toLowerCase();
  if (UNSAFE_METHODS.has(method)) {
    config.headers = config.headers ?? {};
    (config.headers as any)["X-TelePlay-CSRF"] = "1";
  }
  return config;
});

let refreshPromise: Promise<void> | null = null;

const AUTH_NO_REFRESH_PATHS = [
  "/auth/password/login",
  "/auth/code/verify",
  "/auth/code/poll",
  "/auth/link/exchange",
  "/auth/login",
  "/auth/verify-code",
  "/auth/exchange-code",
];

const shouldSkipRefreshForUrl = (url?: string): boolean => {
  if (!url) return false;
  return AUTH_NO_REFRESH_PATHS.some((path) => url.includes(path));
};

const redirectToLogin = () => {
  clearSessionHint();
  if (!window.location.pathname.startsWith("/login")) {
    window.location.href = "/login/password";
  }
};

// Handle expired cookie access tokens. Refresh token is also HttpOnly, so JS
// simply asks the backend to refresh and then retries the original request.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config as any;

    if (error.response?.status === 401 && originalRequest && !originalRequest._retry) {
      if (shouldSkipRefreshForUrl(originalRequest.url)) {
        return Promise.reject(error);
      }

      if (originalRequest.url?.includes("/auth/refresh")) {
        redirectToLogin();
        return Promise.reject(error);
      }

      originalRequest._retry = true;

      try {
        if (!refreshPromise) {
          refreshPromise = api.post("/auth/refresh").then(() => {
            setSessionHint();
          }).finally(() => {
            refreshPromise = null;
          });
        }
        await refreshPromise;
        return api(originalRequest);
      } catch (err) {
        redirectToLogin();
        return Promise.reject(err);
      }
    }

    if (error.response?.status === 429) {
      error.message = "Too many requests. Please wait a moment and try again.";
    }

    return Promise.reject(error);
  },
);

// ============== File type helpers ==============

export const IMAGE_FILE_EXTENSIONS = [
  "jpg",
  "jpeg",
  "png",
  "webp",
  "gif",
  "bmp",
  "avif",
  "svg",
  "ico",
  "tif",
  "tiff",
  "heic",
  "heif",
];

export const getFileExtension = (fileName?: string | null): string => {
  if (!fileName || !fileName.includes(".")) return "";
  return fileName.split(".").pop()?.toLowerCase() || "";
};

export const isImageFile = (
  file: Pick<TelegramFile, "file_type" | "mime_type" | "file_name">,
): boolean => {
  const mimeType = file.mime_type?.toLowerCase() || "";
  return (
    file.file_type === "image" ||
    mimeType.startsWith("image/") ||
    IMAGE_FILE_EXTENSIONS.includes(getFileExtension(file.file_name))
  );
};

export const isTimedMediaFile = (
  file: Pick<TelegramFile, "file_type">,
): boolean => {
  return file.file_type === "video" || file.file_type === "audio";
};

export const isPreviewableFile = (
  file: Pick<TelegramFile, "file_type" | "mime_type" | "file_name">,
): boolean => {
  return isTimedMediaFile(file) || isImageFile(file);
};

export const getVideoResolutionLabel = (
  file: Pick<TelegramFile, "file_type" | "width" | "height">,
): string | null => {
  if (file.file_type !== "video" || !file.width || !file.height) return null;

  const shortSide = Math.min(file.width, file.height);
  const longSide = Math.max(file.width, file.height);

  if (shortSide >= 2160 || longSide >= 3840) return "2160p";
  if (shortSide >= 1440 || longSide >= 2560) return "1440p";
  if (shortSide >= 1080 || longSide >= 1920) return "1080p";
  if (shortSide >= 720 || longSide >= 1280) return "720p";
  if (shortSide >= 480 || longSide >= 854) return "480p";

  return `${file.width}×${file.height}`;
};

// ============== Auth Hooks ==============

export const useCurrentUser = (enabled = true) => {
  return useQuery({
    queryKey: ["currentUser"],
    queryFn: async () => {
      const { data } = await api.get<User>("/auth/me");
      return data;
    },
    enabled,
    retry: false,
  });
};

export const useLoginWithCode = () => {
  return useMutation({
    mutationFn: async (code: string) => {
      const { data } = await api.post<AuthResponse>("/auth/code/verify", { code });
      return data;
    },
  });
};

export const useLoginWithPassword = () => {
  return useMutation({
    mutationFn: async (credentials: PasswordLoginRequest) => {
      const { data } = await api.post<AuthResponse>("/auth/password/login", credentials);
      return data;
    },
  });
};

export const useLogoutAll = () => {
  return useMutation({
    mutationFn: async () => {
      await api.post("/auth/logout-all");
      clearSessionHint();
    },
  });
};

export const sendSessionHeartbeat = async () => {
  await api.post("/auth/session/heartbeat");
};

export const closeTemporarySession = async () => {
  await api.post("/auth/session/close");
};

export const closeTemporarySessionKeepalive = () => {
  const url = `${API_BASE_URL}/auth/session/close`;
  try {
    fetch(url, {
      method: "POST",
      credentials: "include",
      keepalive: true,
      headers: {
        "X-TelePlay-CSRF": "1",
      },
    }).catch(() => undefined);
  } catch {
    // Page is unloading or fetch is unavailable. Heartbeat timeout is the
    // reliable fallback for temporary one-time-login sessions.
  }
};

export const useBotInfo = () => {
  return useQuery({
    queryKey: ["botInfo"],
    queryFn: async () => {
      const { data } = await api.get<BotInfo>("/auth/bot/info");
      return data;
    },
    staleTime: Infinity, // Bot info doesn't change during session
  });
};

export const useGenerateLoginCode = () => {
  return useMutation({
    mutationFn: async () => {
      const { data } = await api.post<LoginCodeResponse>("/auth/code/generate");
      return data;
    },
  });
};

export const useVerifyLoginCode = () => {
  return useMutation({
    mutationFn: async (code: string) => {
      const { data } = await api.post<AuthResponse>("/auth/code/verify", {
        code,
      });
      return data;
    },
  });
};

export const usePollLoginCode = () => {
  return useMutation({
    mutationFn: async (code: string) => {
      const { data } = await api.post<PollCodeResponse>("/auth/code/poll", {
        code,
      });
      return data;
    },
  });
};

export const useWebCredential = () => {
  return useQuery({
    queryKey: ["webCredential"],
    queryFn: async () => {
      const { data } = await api.get<WebCredentialProfile>("/auth/web-credential");
      return data;
    },
    staleTime: 30000,
  });
};

export const useUsernameAvailability = (username: string, enabled = true) => {
  return useQuery({
    queryKey: ["usernameAvailability", username],
    queryFn: async () => {
      const { data } = await api.get<UsernameCheckResponse>("/auth/username/check", {
        params: { username },
      });
      return data;
    },
    enabled: enabled && username.trim().length >= 3,
    staleTime: 10000,
  });
};

export const useChangeUsername = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (username: string) => {
      const { data } = await api.patch<{ message: string }>("/auth/username", { username });
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["currentUser"] });
      queryClient.invalidateQueries({ queryKey: ["webCredential"] });
    },
  });
};

export const useChangePassword = () => {
  return useMutation({
    mutationFn: async (payload: PasswordChangeRequest) => {
      const { data } = await api.post<{ message: string }>("/auth/password/change", payload);
      clearSessionHint();
      return data;
    },
  });
};

export const useAuthSessions = () => {
  return useQuery({
    queryKey: ["authSessions"],
    queryFn: async () => {
      const { data } = await api.get<AuthSession[]>("/auth/sessions");
      return data;
    },
    staleTime: 30000,
  });
};

export const useRevokeAuthSession = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (sessionId: string) => {
      await api.delete(`/auth/sessions/${sessionId}`);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["authSessions"] });
    },
  });
};

export const useRevokeOtherAuthSessions = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await api.delete("/auth/sessions");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["authSessions"] });
    },
  });
};

// ============== Files Hooks ==============

export const useFiles = (
  folderId?: number | null,
  fileType?: string,
  search?: string,
  page = 1,
) => {
  return useQuery({
    queryKey: ["files", folderId, fileType, search, page],
    queryFn: async () => {
      const params: Record<string, any> = {};
      if (folderId !== undefined) params.folder_id = folderId;
      if (fileType) params.file_type = fileType;
      if (search) params.search = search;
      params.page = page;
      params.per_page = 50; // Load 50 files per page
      const { data } = await api.get<FileListResponse>("/files", { params });
      return data;
    },
    staleTime: 60000, // Keep data fresh longer to avoid over-fetching
    gcTime: 5 * 60 * 1000,
    placeholderData: keepPreviousData,
    refetchOnWindowFocus: false,
  });
};

export const useFile = (fileId: number, enabled = true) => {
  return useQuery({
    queryKey: ["file", fileId],
    queryFn: async () => {
      const { data } = await api.get<TelegramFile>(`/files/${fileId}`);
      return data;
    },
    enabled: enabled && !!fileId,
    staleTime: 15000,
    refetchOnWindowFocus: false,
  });
};

export const useUpdateFile = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      ...data
    }: {
      id: number;
      file_name?: string;
      folder_id?: number | null;
    }) => {
      const { data: result } = await api.patch<TelegramFile>(
        `/files/${id}`,
        data,
      );
      return result;
    },
    onSuccess: () => {
      // Invalidate both files and folders to ensure UI updates for moves
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["trash"] });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["folderTree"] });
    },
  });
};

export const useDeleteFile = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      const { data } = await api.delete<{ message: string; recycled: boolean }>(`/files/${id}`);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["trash"] });
    },
  });
};

export const useDeleteFiles = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (ids: number[]) => {
      const { data } = await api.post<{ message: string; recycled: boolean }>("/files/batch-delete", ids as any);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["folders"] }); // Files might be inside folders affecting counts
      queryClient.invalidateQueries({ queryKey: ["storage"] });
      queryClient.invalidateQueries({ queryKey: ["trash"] });
    },
  });
};

export const useMoveFiles = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      ids,
      folderId,
    }: {
      ids: number[];
      folderId: number | null;
    }) => {
      await api.post("/files/batch-move", { ids, folder_id: folderId });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["folderTree"] });
    },
  });
};

export const useRecentFiles = (limit = 20) => {
  return useQuery<FileListResponse>({
    queryKey: ["files", "recent", limit],
    queryFn: async () => {
      const { data } = await api.get<FileListResponse>("/files/recent", {
        params: { limit },
      });
      return data;
    },
    staleTime: 60000,
    gcTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
};

export const useContinueWatching = (limit = 20) => {
  return useQuery<FileListResponse>({
    queryKey: ["files", "continue-watching", limit],
    queryFn: async () => {
      const { data } = await api.get<FileListResponse>(
        "/files/continue-watching",
        { params: { limit } },
      );
      return data;
    },
    staleTime: 30000,
    gcTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
};

export const useStorageStats = () => {
  return useQuery<StorageStats>({
    queryKey: ["storage"],
    queryFn: async () => {
      const { data } = await api.get<StorageStats>("/files/storage");
      return data;
    },
    staleTime: 60000,
  });
};

export const useStorageAnalytics = () =>
  useQuery<StorageAnalytics>({
    queryKey: ["storage", "analytics"],
    queryFn: async () => {
      const { data } = await api.get<StorageAnalytics>("/files/analytics");
      return data;
    },
    staleTime: 30000,
  });

// ============== Recycle Bin Hooks ==============

export const useTrash = () =>
  useQuery<TrashListResponse>({
    queryKey: ["trash"],
    queryFn: async () => {
      const { data } = await api.get<TrashListResponse>("/trash");
      return data;
    },
    staleTime: 15000,
  });

export const useTrashFolder = (folderId: number | null) =>
  useQuery<TrashBrowseResponse>({
    queryKey: ["trash", "folder", folderId],
    queryFn: async () => {
      const { data } = await api.get<TrashBrowseResponse>(
        `/trash/folders/${folderId}/children`,
      );
      return data;
    },
    enabled: folderId !== null,
    staleTime: 15000,
  });

export const useRecycleBinSettings = () =>
  useQuery<RecycleBinSettings>({
    queryKey: ["trash", "settings"],
    queryFn: async () => {
      const { data } = await api.get<RecycleBinSettings>("/trash/settings");
      return data;
    },
    staleTime: 30000,
  });

export const useUpdateRecycleBinSettings = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (settings: RecycleBinSettings) => {
      const { data } = await api.put<RecycleBinSettings>("/trash/settings", settings);
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(["trash", "settings"], data);
      queryClient.invalidateQueries({ queryKey: ["trash"], exact: false });
    },
  });
};

const useTrashMutation = (
  mutationFn: (selection: TrashSelection) => Promise<unknown>,
) => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["trash"] });
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["folderTree"] });
      queryClient.invalidateQueries({ queryKey: ["storage"] });
    },
  });
};

export const useBulkRestoreTrash = () =>
  useTrashMutation(async (selection) => {
    const { data } = await api.post("/trash/bulk-restore", selection);
    return data;
  });

export const useBulkDeleteTrash = () =>
  useTrashMutation(async (selection) => {
    const { data } = await api.post("/trash/bulk-delete", selection);
    return data;
  });

export const useEmptyTrash = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data } = await api.delete("/trash");
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["trash"] });
      queryClient.invalidateQueries({ queryKey: ["storage"] });
    },
  });
};

export const useUpdateProgress = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      fileId,
      position,
      duration,
    }: {
      fileId: number;
      position: number;
      duration?: number;
    }) => {
      await api.post(`/files/${fileId}/progress`, { position, duration });
    },
    onSuccess: (_data, variables) => {
      // Progress is saved every few seconds while playing. Do not refetch
      // all file lists each time; that was slow and wasted server RAM.
      queryClient.setQueryData<TelegramFile>(
        ["file", variables.fileId],
        (old) => (old ? { ...old, last_pos: variables.position } : old),
      );
      queryClient.setQueriesData<FileListResponse>(
        { queryKey: ["files"] },
        (old) => {
          if (!old?.files) return old;
          return {
            ...old,
            files: old.files.map((file) =>
              file.id === variables.fileId
                ? { ...file, last_pos: variables.position }
                : file,
            ),
          };
        },
      );
      queryClient.setQueriesData<TrashListResponse>(
        { queryKey: ["trash"] },
        (old) => {
          if (!old?.files) return old;
          return {
            ...old,
            files: old.files.map((file) =>
              file.id === variables.fileId
                ? { ...file, last_pos: variables.position }
                : file,
            ),
          };
        },
      );

      if (variables.position <= 0) {
        queryClient.invalidateQueries({
          queryKey: ["files", "continue-watching"],
        });
      }
    },
  });
};

export const useClearWatchProgress = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data } = await api.delete<{ cleared: number; message: string }>(
        "/files/progress",
      );
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["file"] });
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({
        queryKey: ["files", "continue-watching"],
      });
      queryClient.invalidateQueries({ queryKey: ["trash"] });
    },
  });
};

// ============== Folders Hooks ==============

export const useMoveFolders = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      ids,
      folderId,
    }: {
      ids: number[];
      folderId: number | null;
    }) => {
      await api.post("/folders/batch-move", { ids, folder_id: folderId });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["folderTree"] });
    },
  });
};

export const useFolders = (parentId?: number | null) => {
  return useQuery({
    queryKey: ["folders", parentId],
    queryFn: async () => {
      const params: Record<string, any> = {};
      if (parentId !== undefined) params.parent_id = parentId;
      const { data } = await api.get<Folder[]>("/folders", { params });
      return data;
    },
    staleTime: 60000, // Folders change less often
  });
};

export const useFolderTree = () => {
  return useQuery({
    queryKey: ["folderTree"],
    queryFn: async () => {
      const { data } = await api.get<Folder[]>("/folders/tree");
      return data;
    },
    staleTime: 60000,
  });
};

export const useCreateFolder = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (data: { name: string; parent_id?: number | null }) => {
      const { data: result } = await api.post<Folder>("/folders", data);
      return result;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["folderTree"] });
    },
  });
};

export const useUpdateFolder = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      ...data
    }: {
      id: number;
      name?: string;
      parent_id?: number | null;
    }) => {
      const { data: result } = await api.patch<Folder>(`/folders/${id}`, data);
      return result;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["folderTree"] });
    },
  });
};

export const useDeleteFolder = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      moveFilesTo,
    }: {
      id: number;
      moveFilesTo?: number | null;
    }) => {
      const params =
        moveFilesTo !== undefined ? { move_files_to: moveFilesTo } : {};
      const { data } = await api.delete<{ message: string; recycled: boolean }>(`/folders/${id}`, { params });
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["folderTree"] });
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["trash"] });
    },
  });
};

export const useDeleteFolders = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (ids: number[]) => {
      const { data } = await api.post<{ message: string; recycled: boolean }>("/folders/batch-delete", ids as any);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      queryClient.invalidateQueries({ queryKey: ["folderTree"] });
      queryClient.invalidateQueries({ queryKey: ["files"] });
      queryClient.invalidateQueries({ queryKey: ["storage"] });
      queryClient.invalidateQueries({ queryKey: ["trash"] });
    },
  });
};

// ============== Utilities ==============

export const formatFileSize = (bytes: number): string => {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex++;
  }
  return `${size.toFixed(1)} ${units[unitIndex]}`;
};

export const formatDuration = (seconds: number | null): string => {
  if (!seconds) return "";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  if (hours > 0) {
    return `${hours}:${minutes.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
  }
  return `${minutes}:${secs.toString().padStart(2, "0")}`;
};

export const getFileIcon = (fileType: string): string => {
  switch (fileType) {
    case "video":
      return "🎬";
    case "audio":
      return "🎵";
    case "image":
      return "🖼️";
    case "document":
      return "📄";
    default:
      return "📎";
  }
};
