"""Central username validation rules for web credentials."""

RESERVED_USERNAMES = {
    "account", "accounts", "ad", "admin", "administrator", "ads", "api", "app",
    "auth", "billing", "bot", "cdn", "contact", "dashboard", "dev", "download",
    "email", "file", "files", "ftp", "help", "home", "hostmaster", "login",
    "logout", "mail", "me", "media", "moderator", "news", "null", "oauth",
    "official", "owner", "privacy", "profile", "root", "security", "settings",
    "signin", "signup", "staff", "status", "stream", "support", "sysadmin",
    "system", "telegram", "teleplay", "telxstream", "test", "undefined", "upload",
    "user", "username", "web", "webmaster", "www",
}

# Keep this intentionally small and transparent. It catches obvious abusive names
# without pretending to be a perfect moderation system.
BANNED_USERNAME_PARTS = {
    "anal", "asshole", "bastard", "bitch", "boobs", "cunt", "dick", "faggot",
    "fuck", "hitler", "nazi", "nigger", "nude", "porn", "pussy", "rape",
    "sex", "shit", "slut", "terror", "whore",
}
