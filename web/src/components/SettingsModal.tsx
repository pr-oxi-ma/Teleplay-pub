import { ReactNode, useEffect, useState } from "react";
import {
  AlertTriangle,
  Eye,
  Gauge,
  LayoutGrid,
  LogOut,
  Monitor,
  PlayCircle,
  RotateCcw,
  SlidersHorizontal,
  Trash2,
  UserCircle,
  X,
} from "lucide-react";
import {
  useAuthSessions,
  useChangePassword,
  useChangeUsername,
  useClearWatchProgress,
  useLogoutAll,
  useRecycleBinSettings,
  useRevokeAuthSession,
  useRevokeOtherAuthSessions,
  useUpdateRecycleBinSettings,
  useUsernameAvailability,
  useWebCredential,
} from "../lib/api";
import {
  ActiveSection,
  AppSettings,
  FileTypeFilter,
  PlaybackMode,
  PLAYBACK_SPEED_OPTIONS,
  ViewMode,
  useAppStore,
} from "../lib/store";

const sanitizeUsernameInput = (value: string) => value.toLowerCase().replace(/\s+/g, "");
const sanitizePasswordInput = (value: string) => value.replace(/\s+/g, "");

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

type SelectOption<T extends string | number> = {
  value: T;
  label: string;
};

const START_SECTION_OPTIONS: SelectOption<ActiveSection>[] = [
  { value: "files", label: "My Files" },
  { value: "recent", label: "Recently Added" },
  { value: "continue_watching", label: "Continue Watching" },
];

const VIEW_MODE_OPTIONS: SelectOption<ViewMode>[] = [
  { value: "grid", label: "Grid" },
  { value: "list", label: "List" },
];

const FILE_FILTER_OPTIONS: SelectOption<string>[] = [
  { value: "all", label: "All files" },
  { value: "video", label: "Videos" },
  { value: "audio", label: "Audio" },
  { value: "image", label: "Images" },
  { value: "document", label: "Documents" },
];

const PLAYBACK_MODE_OPTIONS: SelectOption<PlaybackMode>[] = [
  { value: "normal", label: "Normal" },
  { value: "repeat_one", label: "Repeat one" },
  { value: "shuffle", label: "Shuffle" },
  { value: "autoplay_off", label: "Autoplay off" },
  { value: "stop_after_this", label: "Stop after this" },
];

const fileFilterValue = (value: string): FileTypeFilter =>
  value === "all" ? null : (value as FileTypeFilter);

const SelectSetting = <T extends string | number>({
  label,
  description,
  value,
  options,
  onChange,
}: {
  label: string;
  description?: string;
  value: T;
  options: SelectOption<T>[];
  onChange: (value: T) => void;
}) => (
  <label className="flex flex-col gap-2 rounded-xl border border-white/[0.06] bg-dark-900/50 p-3 sm:flex-row sm:items-center sm:justify-between">
    <span>
      <span className="block text-sm font-semibold text-white">{label}</span>
      {description && (
        <span className="mt-1 block text-xs leading-relaxed text-dark-400">
          {description}
        </span>
      )}
    </span>
    <select
      value={value}
      onChange={(e) => {
        const nextValue =
          typeof value === "number" ? Number(e.target.value) : e.target.value;
        onChange(nextValue as T);
      }}
      className="min-w-[160px] rounded-lg border border-white/[0.08] bg-dark-800 px-3 py-2 text-sm text-white outline-none transition-colors focus:border-primary-400"
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  </label>
);

const ToggleSetting = ({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) => (
  <button
    type="button"
    onClick={() => onChange(!checked)}
    className="flex w-full items-center justify-between gap-3 rounded-xl border border-white/[0.06] bg-dark-900/50 p-3 text-left transition-colors hover:bg-white/[0.03]"
  >
    <span>
      <span className="block text-sm font-semibold text-white">{label}</span>
      {description && (
        <span className="mt-1 block text-xs leading-relaxed text-dark-400">
          {description}
        </span>
      )}
    </span>
    <span
      className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${
        checked ? "bg-primary-500" : "bg-dark-700"
      }`}
    >
      <span
        className={`absolute top-1 h-4 w-4 rounded-full bg-white transition-transform ${
          checked ? "translate-x-6" : "translate-x-1"
        }`}
      />
    </span>
  </button>
);

const SectionHeader = ({
  icon,
  title,
  description,
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) => (
  <div className="mb-4 flex items-start gap-3">
    <div className="rounded-lg bg-primary-500/10 p-2 text-primary-300">
      {icon}
    </div>
    <div>
      <h3 className="font-semibold text-white">{title}</h3>
      <p className="mt-1 text-sm text-dark-400">{description}</p>
    </div>
  </div>
);

export default function SettingsModal({ isOpen, onClose }: SettingsModalProps) {
  const [confirmClearProgress, setConfirmClearProgress] = useState(false);
  const [usernameDraft, setUsernameDraft] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [recycleEnabled, setRecycleEnabled] = useState(true);
  const [retentionDays, setRetentionDays] = useState(30);
  const [retentionChoice, setRetentionChoice] = useState("30");
  const [usernameFormMessage, setUsernameFormMessage] = useState<{
    type: "error" | "success";
    text: string;
  } | null>(null);
  const [passwordFormMessage, setPasswordFormMessage] = useState<{
    type: "error" | "success";
    text: string;
  } | null>(null);
  const clearWatchProgress = useClearWatchProgress();
  const webCredential = useWebCredential();
  const usernameAvailability = useUsernameAvailability(
    usernameDraft,
    usernameDraft.trim().length >= 3 &&
      usernameDraft !== (webCredential.data?.username || ""),
  );
  const changeUsername = useChangeUsername();
  const changePassword = useChangePassword();
  const authSessions = useAuthSessions();
  const revokeAuthSession = useRevokeAuthSession();
  const revokeOtherAuthSessions = useRevokeOtherAuthSessions();
  const logoutAll = useLogoutAll();
  const recycleSettings = useRecycleBinSettings();
  const updateRecycleSettings = useUpdateRecycleBinSettings();
  const { appSettings, updateAppSettings, resetAppSettings, addToast } =
    useAppStore();

  useEffect(() => {
    if (webCredential.data?.username) {
      setUsernameDraft(webCredential.data.username);
    }
  }, [webCredential.data?.username]);

  useEffect(() => {
    setUsernameFormMessage(null);
  }, [usernameDraft]);

  useEffect(() => {
    setPasswordFormMessage(null);
  }, [currentPassword, newPassword]);

  useEffect(() => {
    if (!recycleSettings.data) return;
    const { enabled, retention_days } = recycleSettings.data;
    setRecycleEnabled(enabled);
    setRetentionDays(retention_days);
    setRetentionChoice(
      [3, 7, 14, 30, 60, 90, 180, 365].includes(retention_days)
        ? String(retention_days)
        : "custom",
    );
  }, [recycleSettings.data]);

  if (!isOpen) return null;

  const patchSettings = (settings: Partial<AppSettings>) => {
    updateAppSettings(settings);
  };

  const handleClearWatchProgress = async () => {
    try {
      const result = await clearWatchProgress.mutateAsync();
      const cleared = result?.cleared ?? 0;
      addToast(
        cleared > 0
          ? `Cleared ${cleared} saved progress item${cleared === 1 ? "" : "s"}`
          : "No saved progress to clear",
        "success",
      );
      setConfirmClearProgress(false);
    } catch (error) {
      console.error("Failed to clear watch progress:", error);
      addToast("Failed to clear watch progress", "error");
    }
  };

  const handleResetSettings = () => {
    resetAppSettings();
    addToast("Settings reset", "success");
  };

  const handleSaveRecycleSettings = async () => {
    const safeDays = Math.max(1, Math.min(365, Math.round(retentionDays || 1)));
    try {
      const result = await updateRecycleSettings.mutateAsync({
        enabled: recycleEnabled,
        retention_days: safeDays,
      });
      setRetentionDays(safeDays);
      const updatedItems = result.updated_items || 0;
      addToast(
        updatedItems > 0
          ? `Settings saved · expiry updated for ${updatedItems} Recycle Bin entr${updatedItems === 1 ? "y" : "ies"}`
          : "Recycle Bin settings saved",
        "success",
      );
    } catch (error: any) {
      addToast(error?.response?.data?.detail || "Failed to save Recycle Bin settings", "error");
    }
  };

  const handleChangeUsername = async () => {
    const username = sanitizeUsernameInput(usernameDraft);
    if (!username || username === webCredential.data?.username) return;

    try {
      setUsernameFormMessage(null);
      await changeUsername.mutateAsync(username);
      setUsernameFormMessage({ type: "success", text: "Username updated" });
      addToast("Username updated", "success");
    } catch (error: any) {
      const message =
        error?.response?.data?.detail || "Failed to update username";
      setUsernameFormMessage({ type: "error", text: message });
      addToast(message, "error");
    }
  };

  const handleChangePassword = async () => {
    try {
      setPasswordFormMessage(null);
      await changePassword.mutateAsync({
        currentPassword: sanitizePasswordInput(currentPassword),
        newPassword: sanitizePasswordInput(newPassword),
      });
      setPasswordFormMessage({
        type: "success",
        text: "Password changed. Sign in again.",
      });
      addToast("Password changed. Sign in again.", "success");
      setCurrentPassword("");
      setNewPassword("");
      setTimeout(() => {
        window.location.href = "/login/password";
      }, 500);
    } catch (error: any) {
      const message =
        error?.response?.data?.detail || "Failed to change password";
      setPasswordFormMessage({ type: "error", text: message });
      addToast(message, "error");
    }
  };

  const handleRevokeSession = async (sessionId: string, current: boolean) => {
    try {
      await revokeAuthSession.mutateAsync(sessionId);
      addToast(
        current ? "Current session revoked. Sign in again." : "Session revoked",
        "success",
      );
      if (current) {
        window.location.href = "/login/password";
      }
    } catch (error: any) {
      addToast(
        error?.response?.data?.detail || "Failed to revoke session",
        "error",
      );
    }
  };

  const handleRevokeOtherSessions = async () => {
    try {
      await revokeOtherAuthSessions.mutateAsync();
      addToast("Other sessions revoked", "success");
      authSessions.refetch();
    } catch (error: any) {
      addToast(
        error?.response?.data?.detail || "Failed to revoke other sessions",
        "error",
      );
    }
  };

  const handleLogoutAll = async () => {
    try {
      await logoutAll.mutateAsync();
      addToast("All sessions revoked. Sign in again.", "success");
      window.location.href = "/login/password";
    } catch (error: any) {
      addToast(
        error?.response?.data?.detail || "Failed to revoke sessions",
        "error",
      );
    }
  };

  const formatSessionDate = (value: string) => {
    try {
      return new Date(value).toLocaleString();
    } catch {
      return value;
    }
  };

  const sessionDeviceName = (userAgent: string | null) => {
    if (!userAgent) return "Unknown device";
    if (/android/i.test(userAgent)) return "Android";
    if (/iphone|ipad|ios/i.test(userAgent)) return "iPhone/iPad";
    if (/windows/i.test(userAgent)) return "Windows";
    if (/macintosh|mac os/i.test(userAgent)) return "Mac";
    if (/linux/i.test(userAgent)) return "Linux";
    return "Device";
  };

  const sessionBrowserName = (userAgent: string | null) => {
    if (!userAgent) return "Browser";
    if (/edg\//i.test(userAgent)) return "Edge";
    if (/opr\//i.test(userAgent) || /opera/i.test(userAgent)) return "Opera";
    if (/kiwi/i.test(userAgent)) return "Kiwi Browser";
    if (/chrome|crios/i.test(userAgent)) return "Chrome";
    if (/firefox|fxios/i.test(userAgent)) return "Firefox";
    if (/safari/i.test(userAgent)) return "Safari";
    return "Browser";
  };

  const currentSession = authSessions.data?.find((session) => session.current);
  const isCurrentOneTimeSession = currentSession?.session_type === "temporary";
  const canManageOtherSessions = !isCurrentOneTimeSession;

  return (
    <div
      className="fixed inset-0 z-[110] flex items-start sm:items-center justify-center p-2 sm:p-4 bg-black/60 backdrop-blur-sm animate-fade-in overflow-y-auto"
      onClick={onClose}
    >
      <div
        className="my-2 sm:my-0 flex max-h-[calc(100svh-1rem)] sm:max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-white/10 bg-dark-900 shadow-2xl animate-scale-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b border-white/[0.06] p-4 sm:p-5">
          <div>
            <h2 className="text-xl font-bold text-white">Settings</h2>
            <p className="mt-1 text-sm text-dark-400">
              Web-only settings. Android is untouched.
            </p>
          </div>
          <button
            onClick={onClose}
            className="rounded-full p-2 text-dark-400 transition-colors hover:bg-white/[0.06] hover:text-white"
            title="Close settings"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="space-y-4 sm:space-y-5 overflow-y-auto p-3 sm:p-5">
          <section className="rounded-xl border border-white/[0.06] bg-dark-800/40 p-4">
            <SectionHeader
              icon={<UserCircle className="h-5 w-5" />}
              title="Account"
              description="Manage your permanent web username and password."
            />

            <div className="space-y-4">
              <div className="rounded-xl border border-white/[0.06] bg-dark-900/50 p-3">
                <label className="block text-sm font-semibold text-white mb-2">
                  Username
                </label>
                <p className="mb-2 text-xs text-dark-400">
                  3–16 chars. Starts with a letter. Use lowercase letters, numbers, dot, underscore, or dash.
                </p>
                <div className="flex flex-col gap-2 sm:flex-row">
                  <input
                    value={usernameDraft}
                    onChange={(e) =>
                      setUsernameDraft(sanitizeUsernameInput(e.target.value))
                    }
                    placeholder="username"
                    className="min-w-0 flex-1 rounded-lg border border-white/[0.08] bg-dark-800 px-3 py-2 text-sm text-white outline-none transition-colors focus:border-primary-400"
                  />
                  <button
                    onClick={handleChangeUsername}
                    disabled={
                      changeUsername.isPending ||
                      usernameDraft.trim() === "" ||
                      usernameDraft === (webCredential.data?.username || "") ||
                      usernameAvailability.data?.available === false
                    }
                    className="rounded-lg bg-primary-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {changeUsername.isPending ? "Saving..." : "Save"}
                  </button>
                </div>
                <p className="mt-2 text-xs text-dark-400">
                  Current:{" "}
                  <span className="font-mono text-primary-300">
                    {webCredential.data?.username || "not created"}
                  </span>
                </p>
                {usernameAvailability.data &&
                  usernameDraft !== (webCredential.data?.username || "") && (
                    <p
                      className={`mt-2 text-xs ${usernameAvailability.data.available ? "text-emerald-400" : "text-red-400"}`}
                    >
                      {usernameAvailability.data.available
                        ? "Username is available"
                        : usernameAvailability.data.reason ||
                          "Username is not available"}
                    </p>
                  )}
                {usernameFormMessage && (
                  <div
                    className={`mt-3 rounded-lg px-3 py-2 text-xs ${usernameFormMessage.type === "success" ? "border border-emerald-500/20 bg-emerald-500/10 text-emerald-200" : "border border-red-500/20 bg-red-500/10 text-red-200"}`}
                  >
                    {usernameFormMessage.text}
                  </div>
                )}
              </div>

              <div className="rounded-xl border border-white/[0.06] bg-dark-900/50 p-3">
                <label className="block text-sm font-semibold text-white mb-2">
                  Change password
                </label>
                <p className="mb-2 text-xs text-dark-400">
                  At least 8 characters. Spaces are removed automatically.
                </p>
                <div className="grid gap-2">
                  <input
                    type="password"
                    value={currentPassword}
                    onChange={(e) => setCurrentPassword(sanitizePasswordInput(e.target.value))}
                    placeholder="Current password"
                    autoComplete="current-password"
                    className="rounded-lg border border-white/[0.08] bg-dark-800 px-3 py-2 text-sm text-white outline-none transition-colors focus:border-primary-400"
                  />
                  <input
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(sanitizePasswordInput(e.target.value))}
                    placeholder="New password"
                    autoComplete="new-password"
                    className="rounded-lg border border-white/[0.08] bg-dark-800 px-3 py-2 text-sm text-white outline-none transition-colors focus:border-primary-400"
                  />
                  <button
                    onClick={handleChangePassword}
                    disabled={
                      changePassword.isPending ||
                      !currentPassword ||
                      newPassword.length < 8
                    }
                    className="rounded-lg bg-primary-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {changePassword.isPending
                      ? "Changing..."
                      : "Change password"}
                  </button>
                </div>
                {passwordFormMessage && (
                  <div
                    className={`mt-3 rounded-lg px-3 py-2 text-xs ${passwordFormMessage.type === "success" ? "border border-emerald-500/20 bg-emerald-500/10 text-emerald-200" : "border border-red-500/20 bg-red-500/10 text-red-200"}`}
                  >
                    {passwordFormMessage.text}
                  </div>
                )}
                <p className="mt-2 text-xs text-dark-400">
                  Password change revokes existing sessions and sends you back
                  to login.
                </p>
              </div>

              <div className="rounded-xl border border-white/[0.06] bg-dark-900/50 p-3">
                <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <label className="flex items-center gap-2 text-sm font-semibold text-white">
                    <Monitor className="h-4 w-4 text-primary-300" /> Active
                    sessions
                  </label>
                  <div className="flex flex-wrap gap-2">
                    <button
                      onClick={() => authSessions.refetch()}
                      className="rounded-lg border border-white/[0.08] px-3 py-1 text-xs font-medium text-dark-300 transition-colors hover:border-primary-400 hover:text-white"
                    >
                      Refresh
                    </button>
                    <button
                      onClick={handleRevokeOtherSessions}
                      disabled={
                        revokeOtherAuthSessions.isPending ||
                        !canManageOtherSessions ||
                        !authSessions.data?.some((session) => !session.current)
                      }
                      title={
                        canManageOtherSessions
                          ? "Revoke every session except this one"
                          : "One-time login sessions can only revoke themselves"
                      }
                      className="rounded-lg border border-white/[0.08] px-3 py-1 text-xs font-medium text-dark-300 transition-colors hover:border-primary-400 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Revoke others
                    </button>
                    <button
                      onClick={handleLogoutAll}
                      disabled={logoutAll.isPending || !canManageOtherSessions}
                      title={
                        canManageOtherSessions
                          ? "Revoke all sessions and sign out"
                          : "One-time login sessions can only revoke themselves"
                      }
                      className="rounded-lg border border-red-400/20 px-3 py-1 text-xs font-medium text-red-300 transition-colors hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Logout all
                    </button>
                  </div>
                </div>

                {isCurrentOneTimeSession && (
                  <div className="mb-3 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                    You are signed in with a one-time session. It can revoke only itself; sign in with username/password to manage other sessions.
                  </div>
                )}

                {authSessions.isLoading ? (
                  <p className="text-xs text-dark-400">Loading sessions...</p>
                ) : authSessions.data?.length ? (
                  <div className="space-y-2">
                    {authSessions.data.map((session) => {
                      const canRevokeThisSession =
                        canManageOtherSessions || session.current;

                      return (
                        <div
                          key={session.session_id}
                          className="rounded-lg border border-white/[0.06] bg-dark-800/70 p-3"
                        >
                          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                            <div className="min-w-0 flex-1">
                              <p className="break-words text-sm font-semibold leading-6 text-white">
                                {sessionBrowserName(session.user_agent)} on{" "}
                                {sessionDeviceName(session.user_agent)}
                              </p>
                              <div className="mt-2 flex flex-wrap gap-2">
                                {session.current && (
                                  <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] uppercase tracking-wide text-emerald-300">
                                    Current
                                  </span>
                                )}
                                {session.session_type === "temporary" && (
                                  <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] uppercase tracking-wide text-amber-300">
                                    One-time
                                  </span>
                                )}
                              </div>
                              <p
                                className="mt-2 break-all text-xs text-dark-400 sm:truncate"
                                title={session.user_agent || undefined}
                              >
                                {session.user_agent || "No user agent"}
                              </p>
                              <p className="mt-1 text-xs leading-5 text-dark-500">
                                Last used: {formatSessionDate(session.last_used_at)}
                                <span className="hidden sm:inline"> · </span>
                                <br className="sm:hidden" />
                                Expires: {formatSessionDate(session.expires_at)}
                              </p>
                            </div>
                            {canRevokeThisSession ? (
                              <button
                                onClick={() =>
                                  handleRevokeSession(
                                    session.session_id,
                                    session.current,
                                  )
                                }
                                disabled={revokeAuthSession.isPending}
                                className="inline-flex w-full items-center justify-center rounded-lg border border-red-400/20 px-3 py-2 text-xs font-semibold text-red-300 transition-colors hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-50 sm:w-auto"
                                title={
                                  session.current
                                    ? "Revoking current session signs you out"
                                    : "Revoke this session"
                                }
                              >
                                <LogOut className="mr-1.5 h-3.5 w-3.5" />
                                Revoke
                              </button>
                            ) : (
                              <span className="rounded-lg border border-white/[0.06] px-3 py-2 text-center text-xs text-dark-500 sm:text-left">
                                Sign in with username/password to revoke
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-xs text-dark-400">
                    No active sessions found.
                  </p>
                )}
              </div>
            </div>
          </section>

          <section className="rounded-xl border border-white/[0.06] bg-dark-800/40 p-4">
            <SectionHeader
              icon={<LayoutGrid className="h-5 w-5" />}
              title="Library"
              description="Control how files open and how file cards look."
            />

            <div className="space-y-3">
              <SelectSetting
                label="Default start page"
                description="The section TelePlay opens first."
                value={appSettings.defaultStartSection}
                options={START_SECTION_OPTIONS}
                onChange={(value) =>
                  patchSettings({ defaultStartSection: value })
                }
              />
              <SelectSetting
                label="Default view"
                description="Applies immediately and is used on next open."
                value={appSettings.defaultViewMode}
                options={VIEW_MODE_OPTIONS}
                onChange={(value) => patchSettings({ defaultViewMode: value })}
              />
              <SelectSetting
                label="Default file filter"
                description="Choose the default All / Video / Audio / Image / Document filter."
                value={appSettings.defaultFileTypeFilter ?? "all"}
                options={FILE_FILTER_OPTIONS}
                onChange={(value) =>
                  patchSettings({
                    defaultFileTypeFilter: fileFilterValue(value),
                  })
                }
              />
              <ToggleSetting
                label="Show video resolution"
                description="Uses existing width/height metadata only. No server download or probing."
                checked={appSettings.showVideoResolution}
                onChange={(checked) =>
                  patchSettings({ showVideoResolution: checked })
                }
              />
            </div>
          </section>

          <section className="rounded-xl border border-white/[0.06] bg-dark-800/40 p-4">
            <SectionHeader
              icon={<Gauge className="h-5 w-5" />}
              title="Playback"
              description="Default player behavior for video and audio."
            />

            <div className="space-y-3">
              <ToggleSetting
                label="Resume playback"
                description="When off, videos and audio always start from 0 and progress is not saved."
                checked={appSettings.resumePlayback}
                onChange={(checked) =>
                  patchSettings({ resumePlayback: checked })
                }
              />
              <SelectSetting
                label="Default playback speed"
                value={appSettings.defaultPlaybackSpeed}
                options={PLAYBACK_SPEED_OPTIONS.map((speed) => ({
                  value: speed,
                  label: `${speed}x`,
                }))}
                onChange={(value) =>
                  patchSettings({ defaultPlaybackSpeed: value })
                }
              />
              <SelectSetting
                label="Playback mode"
                description="Same modes as the player button."
                value={appSettings.playbackMode}
                options={PLAYBACK_MODE_OPTIONS}
                onChange={(value) => patchSettings({ playbackMode: value })}
              />
            </div>
          </section>

          <section className="rounded-xl border border-white/[0.06] bg-dark-800/40 p-4">
            <SectionHeader
              icon={<Trash2 className="h-5 w-5" />}
              title="Recycle Bin"
              description="Control whether deletes are recoverable and how long deleted items are kept."
            />

            {recycleSettings.isLoading ? (
              <div className="h-28 animate-pulse rounded-xl bg-dark-900/50" />
            ) : (
              <div className="space-y-3">
                <ToggleSetting
                  label="Enable Recycle Bin"
                  description="When disabled, new deletes are permanent. Existing Recycle Bin items stay available until their expiry date."
                  checked={recycleEnabled}
                  onChange={setRecycleEnabled}
                />

                <div className="rounded-xl border border-white/[0.06] bg-dark-900/50 p-3">
                  <label className="block text-sm font-semibold text-white">
                    Keep deleted items for
                  </label>
                  <p className="mt-1 text-xs leading-relaxed text-dark-400">
                    Changing this also recalculates the expiry date of items already in Recycle Bin.
                  </p>
                  <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                    <select
                      value={retentionChoice}
                      onChange={(event) => {
                        const value = event.target.value;
                        setRetentionChoice(value);
                        if (value !== "custom") setRetentionDays(Number(value));
                      }}
                      className="min-w-[180px] flex-1 rounded-lg border border-white/[0.08] bg-dark-800 px-3 py-2 text-sm text-white outline-none transition-colors focus:border-primary-400"
                    >
                      {[3, 7, 14, 30, 60, 90, 180, 365].map((days) => (
                        <option key={days} value={days}>
                          {days} days{days === 30 ? " (default)" : ""}
                        </option>
                      ))}
                      <option value="custom">Custom days…</option>
                    </select>
                    {retentionChoice === "custom" && (
                      <div className="flex items-center gap-2 sm:w-44">
                        <input
                          type="number"
                          min={1}
                          max={365}
                          inputMode="numeric"
                          value={retentionDays}
                          onChange={(event) => setRetentionDays(Number(event.target.value))}
                          className="w-full rounded-lg border border-white/[0.08] bg-dark-800 px-3 py-2 text-sm text-white outline-none transition-colors focus:border-primary-400"
                        />
                        <span className="text-xs text-dark-400">days</span>
                      </div>
                    )}
                  </div>
                  <p className="mt-2 text-xs text-dark-500">Allowed range: 1–365 days.</p>
                </div>

                <button
                  onClick={handleSaveRecycleSettings}
                  disabled={updateRecycleSettings.isPending || retentionDays < 1 || retentionDays > 365}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-lg bg-primary-600 px-4 py-2.5 text-sm font-semibold text-white shadow-lg shadow-primary-500/20 transition-colors hover:bg-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {updateRecycleSettings.isPending ? "Saving…" : "Save Recycle Bin settings"}
                </button>
              </div>
            )}
          </section>

          <section className="rounded-xl border border-white/[0.06] bg-dark-800/40 p-4">
            <SectionHeader
              icon={<PlayCircle className="h-5 w-5" />}
              title="Continue Watching"
              description="Clear all saved resume positions. Files are not deleted."
            />

            {!confirmClearProgress ? (
              <button
                onClick={() => setConfirmClearProgress(true)}
                className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-2.5 text-sm font-semibold text-red-300 transition-colors hover:bg-red-500/20"
              >
                <Trash2 className="h-4 w-4" />
                Clear watch progress
              </button>
            ) : (
              <div className="rounded-xl border border-red-500/20 bg-red-500/10 p-3">
                <div className="mb-3 flex items-start gap-2 text-sm text-red-100">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-300" />
                  <span>
                    Are you sure? All saved resume positions will be removed.
                  </span>
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => setConfirmClearProgress(false)}
                    className="flex-1 rounded-lg px-3 py-2 text-sm font-medium text-dark-200 transition-colors hover:bg-white/[0.06]"
                    disabled={clearWatchProgress.isPending}
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleClearWatchProgress}
                    className="flex-1 rounded-lg bg-red-500 px-3 py-2 text-sm font-semibold text-white transition-colors hover:bg-red-600 disabled:cursor-not-allowed disabled:opacity-60"
                    disabled={clearWatchProgress.isPending}
                  >
                    {clearWatchProgress.isPending ? "Clearing..." : "Clear"}
                  </button>
                </div>
              </div>
            )}
          </section>

          <section className="rounded-xl border border-white/[0.06] bg-dark-800/40 p-4">
            <SectionHeader
              icon={<SlidersHorizontal className="h-5 w-5" />}
              title="Reset"
              description="Restore TelePlay web settings to default."
            />
            <button
              onClick={handleResetSettings}
              className="inline-flex w-full items-center justify-center gap-2 rounded-lg border border-white/[0.08] bg-dark-900/70 px-4 py-2.5 text-sm font-semibold text-dark-200 transition-colors hover:bg-white/[0.06] hover:text-white"
            >
              <RotateCcw className="h-4 w-4" />
              Reset settings
            </button>
          </section>

          <div className="flex items-start gap-2 rounded-xl border border-primary-500/15 bg-primary-500/10 p-3 text-xs text-primary-100">
            <Eye className="mt-0.5 h-4 w-4 shrink-0" />
            <p>
              Resolution is displayed from Telegram metadata already saved in
              the database. FPS and codec are still skipped because they need
              server-side probing/download.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
