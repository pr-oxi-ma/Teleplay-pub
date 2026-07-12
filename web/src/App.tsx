import {
  Routes,
  Route,
  Navigate,
  useSearchParams,
  useNavigate,
  Link,
} from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  clearSessionHint,
  closeTemporarySessionKeepalive,
  sendSessionHeartbeat,
  setSessionHint,
  toApiUrl,
  useBotInfo,
  useCurrentUser,
  useGenerateLoginCode,
  useLoginWithCode,
  useLoginWithPassword,
  usePollLoginCode,
} from "./lib/api";
import FileBrowser from "./components/FileBrowser";
import GlobalContextMenu from "./components/GlobalContextMenu";
import MediaPlayer from "./components/MediaPlayer";
import logo from "./assets/logo.png";

const sanitizeUsernameInput = (value: string) => value.toLowerCase().replace(/\s+/g, "");
const sanitizePasswordInput = (value: string) => value.replace(/\s+/g, "");

function AuthCallback() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const code = searchParams.get("code");
  const legacyToken = searchParams.get("token");
  const [status, setStatus] = useState("Processing...");
  const hasProcessedCode = useRef(false);

  useEffect(() => {
    if (hasProcessedCode.current) return;
    hasProcessedCode.current = true;
    window.history.replaceState({}, document.title, "/auth");

    const redirectToPasswordLogin = () => {
      setTimeout(() => navigate("/login/password", { replace: true }), 1200);
    };

    if (!code) {
      clearSessionHint();
      setStatus(
        legacyToken
          ? "❌ Old token links are disabled. Redirecting to login..."
          : "❌ No login code in URL. Redirecting to login...",
      );
      redirectToPasswordLogin();
      return;
    }

    api
      .post("/auth/link/exchange", { code })
      .then(() => {
        setSessionHint();
        setStatus("✅ Logged in! Redirecting...");
        setTimeout(() => navigate("/", { replace: true }), 300);
      })
      .catch((err: any) => {
        clearSessionHint();
        const detail =
          err.response?.data?.detail || "Login link expired or already used";
        setStatus(`❌ ${detail}. Redirecting to login...`);
        redirectToPasswordLogin();
      });
  }, [code, legacyToken, navigate]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-dark-950 p-4">
      <div className="text-center max-w-lg">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary-500 mx-auto mb-4"></div>
        <p className="text-white text-lg mb-4">{status}</p>
      </div>
    </div>
  );
}

function LoginShell({
  children,
  active,
}: {
  children: React.ReactNode;
  active: "password" | "code";
}) {
  return (
    <div className="min-h-[100svh] flex items-start sm:items-center justify-center p-3 sm:p-4 relative overflow-x-hidden overflow-y-auto">
      <div className="absolute inset-0 bg-dark-950">
        <div className="absolute top-0 left-1/4 w-96 h-96 bg-primary-600/20 rounded-full blur-3xl"></div>
        <div className="absolute bottom-0 right-1/4 w-96 h-96 bg-primary-500/10 rounded-full blur-3xl"></div>
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[600px] h-[600px] bg-primary-700/5 rounded-full blur-3xl"></div>
      </div>

      <div className="glass-panel p-4 sm:p-8 max-w-md w-full text-center animate-scale-in relative z-10 my-3 sm:my-0">
        <img
          src={logo}
          alt="TelePlay"
          className="w-16 h-16 sm:w-24 sm:h-24 mx-auto mb-3 sm:mb-6 drop-shadow-2xl"
        />
        <h1 className="text-2xl sm:text-3xl font-bold mb-1 sm:mb-2 text-gradient">
          TelePlay
        </h1>
        <p className="text-sm sm:text-base text-dark-400 mb-4 sm:mb-6">
          Stream your files from Telegram
        </p>

        <div className="grid grid-cols-2 gap-2 mb-4 sm:mb-6 rounded-2xl bg-dark-900/60 p-1 border border-white/[0.06]">
          <Link
            to="/login/password"
            className={`rounded-xl py-2 text-sm font-medium transition-colors ${active === "password" ? "bg-primary-600 text-white" : "text-dark-400 hover:text-white"}`}
          >
            Username
          </Link>
          <Link
            to="/login/code"
            className={`rounded-xl py-2 text-sm font-medium transition-colors ${active === "code" ? "bg-primary-600 text-white" : "text-dark-400 hover:text-white"}`}
          >
            Code
          </Link>
        </div>

        {children}
      </div>
    </div>
  );
}

function PasswordLoginPage() {
  const { mutate: loginWithPassword, isPending } = useLoginWithPassword();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const handlePasswordLogin = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    loginWithPassword(
      { username: sanitizeUsernameInput(username), password: sanitizePasswordInput(password) },
      {
        onSuccess: () => {
          setSessionHint();
          window.location.href = "/";
        },
        onError: () => {
          clearSessionHint();
          setError("Invalid credentials");
        },
      },
    );
  };

  return (
    <LoginShell active="password">
      <div className="glass-card p-4 sm:p-6">
        <h3 className="text-white font-medium mb-4 flex items-center justify-center gap-2">
          <svg
            className="w-5 h-5 text-primary-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M12 11c1.657 0 3-1.343 3-3S13.657 5 12 5 9 6.343 9 8s1.343 3 3 3zm0 2c-2.21 0-4 1.343-4 3v1h8v-1c0-1.657-1.79-3-4-3z"
            />
          </svg>
          Login with Username
        </h3>
        <form onSubmit={handlePasswordLogin} className="flex flex-col gap-3">
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(sanitizeUsernameInput(e.target.value))}
            placeholder="Username from /setlogin"
            autoComplete="username"
            className="w-full bg-dark-800 border border-white/10 rounded-xl px-4 py-3 text-white placeholder-dark-500 focus:border-primary-500 focus:outline-none transition-colors text-base"
          />
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(sanitizePasswordInput(e.target.value))}
            placeholder="Password"
            autoComplete="current-password"
            className="w-full bg-dark-800 border border-white/10 rounded-xl px-4 py-3 text-white placeholder-dark-500 focus:border-primary-500 focus:outline-none transition-colors text-base"
          />
          <button
            type="submit"
            disabled={isPending || !username.trim() || !password}
            className="btn-primary w-full disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isPending ? "Signing in..." : "Sign In"}
          </button>
          {error && <p className="text-red-400 text-sm mt-1">{error}</p>}
        </form>
        <p className="text-xs text-dark-500 mt-4">
          Create credentials in Telegram with{" "}
          <span className="text-primary-400 font-mono bg-dark-800/50 px-1.5 py-0.5 rounded">
            /setlogin username
          </span>
          <span className="block mt-1 text-dark-500">
            Username: 3–16 chars, starts with a letter. Password spaces are removed automatically.
          </span>
        </p>
      </div>
    </LoginShell>
  );
}

function CodeLoginPage() {
  const { mutate: loginByCode, isPending: isVerifying } = useLoginWithCode();
  const { mutate: generateCode, isPending: isGenerating } =
    useGenerateLoginCode();
  const { mutate: pollCode } = usePollLoginCode();
  const [code, setCode] = useState("");
  const [isPolling, setIsPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generatedAt, setGeneratedAt] = useState<number | null>(null);
  const hasGeneratedCode = useRef(false);
  const pollStartedAtRef = useRef<number | null>(null);
  const pollAttemptsRef = useRef(0);

  const startNewCode = useCallback(() => {
    setError(null);
    setIsPolling(false);
    setCode("");
    pollStartedAtRef.current = null;
    pollAttemptsRef.current = 0;

    generateCode(undefined, {
      onSuccess: (data) => {
        setCode(data.code);
        setGeneratedAt(Date.now());
        setIsPolling(true);
      },
      onError: (err: any) => {
        setError(err.response?.data?.detail || "Failed to generate code");
      },
    });
  }, [generateCode]);

  useEffect(() => {
    if (hasGeneratedCode.current) return;
    hasGeneratedCode.current = true;
    startNewCode();
  }, [startNewCode]);

  useEffect(() => {
    if (!isPolling || code.length !== 6) return;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (!pollStartedAtRef.current) pollStartedAtRef.current = Date.now();

    const scheduleNextPoll = () => {
      if (cancelled) return;

      const elapsed = Date.now() - (pollStartedAtRef.current || Date.now());
      if (elapsed > 5 * 60 * 1000) {
        setIsPolling(false);
        setError("Login code expired. Generate a new code.");
        return;
      }

      const delay = elapsed < 60 * 1000 ? 3000 : 6000;
      timer = setTimeout(runPoll, delay);
    };

    const runPoll = () => {
      if (cancelled) return;

      if (document.hidden) {
        scheduleNextPoll();
        return;
      }

      pollAttemptsRef.current += 1;
      pollCode(code, {
        onSuccess: (data) => {
          if (cancelled) return;
          if (data.status === "claimed") {
            setSessionHint();
            setIsPolling(false);
            window.location.href = "/";
            return;
          }
          scheduleNextPoll();
        },
        onError: (err: any) => {
          if (cancelled) return;
          const status = err.response?.status;
          if (status === 400 || status === 410 || status === 429) {
            clearSessionHint();
            setIsPolling(false);
            setError(
              err.response?.data?.detail || "Login code expired or invalid",
            );
            return;
          }
          scheduleNextPoll();
        },
      });
    };

    scheduleNextPoll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [isPolling, code, pollCode]);

  const handleManualLogin = (e: React.FormEvent) => {
    e.preventDefault();
    if (!code) return;

    loginByCode(code, {
      onSuccess: () => {
        setSessionHint();
        window.location.href = "/";
      },
      onError: (err: any) => {
        clearSessionHint();
        setError(err.response?.data?.detail || "Invalid code");
      },
    });
  };

  const secondsLeft = generatedAt
    ? Math.max(
        0,
        Math.ceil((5 * 60 * 1000 - (Date.now() - generatedAt)) / 1000),
      )
    : null;

  return (
    <LoginShell active="code">
      <div className="glass-card p-4 sm:p-6">
        <h3 className="text-white font-medium mb-4 flex items-center justify-center gap-2">
          <svg
            className="w-5 h-5 text-primary-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"
            />
          </svg>
          Login with Code
        </h3>
        <form onSubmit={handleManualLogin} className="flex flex-col gap-3">
          <div className="relative">
            <input
              type="text"
              placeholder="ENTER 6-DIGIT CODE"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              maxLength={6}
              className="w-full bg-dark-900/60 border border-white/[0.08] rounded-xl px-4 py-3 sm:py-4 text-center text-xl tracking-[0.1em] sm:text-2xl sm:tracking-[0.3em] font-mono text-white placeholder:text-sm placeholder:tracking-normal placeholder:font-sans placeholder-dark-600 focus:outline-none focus:ring-2 focus:ring-primary-500/50 focus:border-primary-500/50 transition-all duration-200 uppercase"
            />
            {isGenerating && (
              <div className="absolute inset-0 flex items-center justify-center bg-dark-900/40 rounded-xl">
                <div className="w-5 h-5 border-2 border-primary-500/30 border-t-primary-500 rounded-full animate-spin"></div>
              </div>
            )}
          </div>
          <button
            type="submit"
            disabled={code.length < 6 || isVerifying}
            className="btn-primary w-full py-3 text-base disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isVerifying ? "Verifying..." : "Login"}
          </button>
          {error && (
            <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-200">
              {error}
            </div>
          )}
          {error && !isPolling && (
            <button
              type="button"
              onClick={startNewCode}
              disabled={isGenerating}
              className="btn-secondary w-full py-2.5 text-sm disabled:opacity-50"
            >
              {isGenerating ? "Generating..." : "Generate new code"}
            </button>
          )}
        </form>

        {isPolling && (
          <div className="flex flex-col items-center justify-center gap-1 mt-4 text-xs text-dark-400">
            <div className="flex items-center gap-2">
              <div className="w-2 h-2 bg-primary-500 rounded-full animate-pulse"></div>
              Waiting for Telegram confirmation...
            </div>
            <span className="text-dark-500">
              Polling slows down after 1 minute and stops after 5 minutes.
              {secondsLeft !== null
                ? ` About ${Math.ceil(secondsLeft / 60)} min left.`
                : ""}
            </span>
          </div>
        )}

        <p className="text-xs text-dark-500 mt-4 leading-relaxed">
          Send{" "}
          <span className="text-primary-400 font-mono bg-dark-800/50 px-1.5 py-0.5 rounded">
            /login {code || "CODE"}
          </span>{" "}
          to the bot to confirm this code.
        </p>
      </div>

      <div className="relative my-4 sm:my-6">
        <div className="absolute inset-0 flex items-center">
          <div className="w-full border-t border-white/[0.06]"></div>
        </div>
        <div className="relative flex justify-center text-xs uppercase">
          <span className="bg-dark-900/80 px-3 text-dark-500">
            Or open bot directly
          </span>
        </div>
      </div>

      <BotLink code={code} />
    </LoginShell>
  );
}

function BotLink({ code }: { code?: string }) {
  const { data: botInfo } = useBotInfo();
  const botUrl = botInfo?.username
    ? `https://t.me/${botInfo.username}${code ? `?start=${code}` : ""}`
    : "#";

  return (
    <a
      href={botUrl}
      target="_blank"
      rel="noopener noreferrer"
      className={`btn-secondary inline-flex items-center justify-center gap-2 w-full py-3 ${!botInfo?.username ? "opacity-50 pointer-events-none" : ""}`}
    >
      <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
        <path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.562 8.161c-.18 1.897-.962 6.502-1.359 8.627-.168.9-.5 1.201-.82 1.23-.697.064-1.226-.461-1.901-.903-1.056-.692-1.653-1.123-2.678-1.799-1.185-.781-.417-1.21.258-1.911.177-.184 3.247-2.977 3.307-3.23.007-.032.015-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.139-5.062 3.345-.479.329-.913.489-1.302.481-.428-.009-1.252-.242-1.865-.442-.751-.244-1.349-.374-1.297-.789.027-.216.324-.437.893-.663 3.498-1.524 5.831-2.529 6.998-3.015 3.333-1.386 4.025-1.627 4.477-1.635.099-.002.321.023.465.141.121.099.155.232.17.325.015.094.034.31.019.478z" />
      </svg>
      Open Telegram Bot
    </a>
  );
}

function useSessionHeartbeat(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;

    let stopped = false;

    const heartbeat = () => {
      if (stopped || document.hidden) return;
      sendSessionHeartbeat().catch(() => undefined);
    };

    // Run once after auth is confirmed, then keep temp one-time-login sessions
    // alive while the tab is active. Persistent username/password sessions also
    // tolerate this endpoint; the backend only auto-revokes temporary sessions.
    heartbeat();
    const interval = window.setInterval(heartbeat, 60_000);

    const handleVisibilityChange = () => {
      if (!document.hidden) heartbeat();
    };

    const handlePageHide = () => {
      // Best-effort close for temporary sessions. If mobile kills the browser
      // before this request finishes, backend heartbeat timeout still handles it.
      closeTemporarySessionKeepalive();
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("pagehide", handlePageHide);

    return () => {
      stopped = true;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("pagehide", handlePageHide);
    };
  }, [enabled]);
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { data: user, isLoading, error } = useCurrentUser(true);
  const isAuthenticated = !!user && !isLoading && !error;

  useSessionHeartbeat(isAuthenticated);

  useEffect(() => {
    if (user) setSessionHint();
  }, [user]);

  if (isLoading) {
    return <div className="min-h-screen bg-dark-950" />;
  }

  if (error) {
    clearSessionHint();
    return <Navigate to="/login/password" replace />;
  }

  return <>{children}</>;
}

function PublicOnlyRoute({ children }: { children: React.ReactNode }) {
  const [checking, setChecking] = useState(true);
  const [authenticated, setAuthenticated] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const checkSession = async () => {
      const requestMe = () =>
        fetch(toApiUrl("/api/auth/me"), { credentials: "include" });
      try {
        let response = await requestMe();
        if (response.status === 401) {
          const refresh = await fetch(toApiUrl("/api/auth/refresh"), {
            method: "POST",
            credentials: "include",
            headers: { "X-TelePlay-CSRF": "1" },
          });
          if (refresh.ok) response = await requestMe();
        }
        if (!cancelled && response.ok) {
          setSessionHint();
          setAuthenticated(true);
        }
      } catch {
        // No valid cookie session: keep the public auth page available.
      } finally {
        if (!cancelled) setChecking(false);
      }
    };
    checkSession();
    return () => {
      cancelled = true;
    };
  }, []);

  if (checking) {
    return <div className="min-h-screen bg-dark-950" />;
  }

  if (authenticated) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}

function App() {
  return (
    <>
      <GlobalContextMenu />
      <MediaPlayer />
      <Routes>
        <Route
          path="/login"
          element={<PublicOnlyRoute><Navigate to="/login/password" replace /></PublicOnlyRoute>}
        />
        <Route path="/login/password" element={<PublicOnlyRoute><PasswordLoginPage /></PublicOnlyRoute>} />
        <Route path="/login/code" element={<PublicOnlyRoute><CodeLoginPage /></PublicOnlyRoute>} />
        <Route path="/auth" element={<PublicOnlyRoute><AuthCallback /></PublicOnlyRoute>} />
        <Route
          path="/*"
          element={
            <ProtectedRoute>
              <FileBrowser />
            </ProtectedRoute>
          }
        />
      </Routes>
    </>
  );
}

export default App;
