"""FastAPI application: Plex sign-in, setup, and the read-only playlist views (M1)."""

import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from .. import __version__
from ..config import Config
from ..crypto import Secrets
from ..db import get_app_settings, make_engine
from ..models import AppSettings, HomeUser, PlaylistBackup, ShareGroup
from ..plex.auth import Pin, PlexAuth, PlexAuthError
from ..plex.gateway import Gateway, PlexApiGateway, PlexError
from ..sharing import Mode, ShareError, ShareService
from ..users import UserNeedsAttention, UserService

HERE = Path(__file__).parent
PIN_TIMEOUT_S = 300
MIN_INTERVAL, MAX_INTERVAL = 15, 3600

GatewayFactory = Callable[[str, str], Gateway]


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


def create_app(config: Config | None = None, *, gateway_factory: GatewayFactory = PlexApiGateway,
               plex_auth: PlexAuth | None = None, db_url: str | None = None) -> FastAPI:
    config = config or Config()
    secrets = Secrets(config.key_path)
    engine: Engine = make_engine(db_url or f"sqlite:///{config.db_path}")
    plex_auth = plex_auth or PlexAuth()
    gateways: dict[tuple[str, str], Gateway] = {}

    app = FastAPI(title="Syncalist", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=secrets.session_secret,
                       session_cookie="syncalist_session", max_age=30 * 24 * 3600, same_site="lax")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["ago"] = _ago
    templates.env.globals["version"] = __version__

    # -- dependencies --------------------------------------------------------------
    def db():
        with Session(engine) as session:
            yield session

    def app_settings(session: Session = Depends(db)) -> AppSettings:
        return get_app_settings(session, config.sync_interval)

    def gateway_for(settings: AppSettings, token: str | None = None) -> Gateway:
        token = token or secrets.decrypt(settings.admin_token_enc)
        if not settings.plex_url or not token:
            raise Redirect("/setup")
        key = (settings.plex_url, token)
        if key not in gateways:
            gateways.clear()  # only ever one admin/server; drop stale ones
            gateways[key] = gateway_factory(settings.plex_url, token)
        return gateways[key]

    def require_admin(request: Request, settings: AppSettings = Depends(app_settings)) -> AppSettings:
        account_id = request.session.get("account_id")
        if not account_id:
            raise Redirect("/login")
        if settings.admin_account_id is None or not settings.is_configured:
            raise Redirect("/setup")
        if account_id != settings.admin_account_id:
            request.session.clear()
            raise Redirect("/login")
        return settings

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("signed_in", bool(request.session.get("account_id")))
        ctx["flashes"] = request.session.pop("flashes", [])
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def flash(request: Request, message: str, kind: str = "info") -> None:
        # Assign (don't mutate in place) so the session middleware sees the change.
        request.session["flashes"] = [*request.session.get("flashes", []), {"kind": kind, "message": message}]

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)

    # -- health ----------------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return JSONResponse({"status": "ok", "version": __version__})

    # -- sign in ---------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request, settings: AppSettings = Depends(app_settings)):
        if request.session.get("account_id") and request.session.get("account_id") == settings.admin_account_id:
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", first_run=settings.admin_account_id is None,
                      server_name=settings.server_name, signed_in=False)

    @app.get("/auth/start")
    def auth_start(request: Request, settings: AppSettings = Depends(app_settings)):
        try:
            pin = plex_auth.create_pin(settings.client_id)
        except PlexAuthError as e:
            flash(request, str(e), "error")
            return RedirectResponse("/login", status_code=303)
        request.session["pin"] = {"id": pin.id, "code": pin.code, "started": time.time()}
        forward = str(request.url_for("auth_callback"))
        return RedirectResponse(plex_auth.auth_url(settings.client_id, pin, forward), status_code=303)

    @app.get("/auth/callback", response_class=HTMLResponse, name="auth_callback")
    def auth_callback(request: Request, session: Session = Depends(db),
                      settings: AppSettings = Depends(app_settings)):
        pin = request.session.get("pin")
        if not pin:
            return RedirectResponse("/login", status_code=303)
        try:
            token = plex_auth.check_pin(settings.client_id, pin["id"])
            if not token:
                if time.time() - pin["started"] > PIN_TIMEOUT_S:
                    request.session.pop("pin", None)
                    flash(request, "Sign-in timed out. Please try again.", "error")
                    return RedirectResponse("/login", status_code=303)
                return render(request, "waiting.html", signed_in=False,
                              auth_url=plex_auth.auth_url(settings.client_id, Pin(pin["id"], pin["code"]),
                                                          str(request.url_for("auth_callback"))))
            account = plex_auth.account(settings.client_id, token)
        except PlexAuthError as e:
            request.session.pop("pin", None)
            flash(request, str(e), "error")
            return RedirectResponse("/login", status_code=303)
        request.session.pop("pin", None)

        if settings.admin_account_id is None:
            # First run: this account sets the app up. /setup checks it owns the server.
            request.session["account_id"] = account.id
            request.session["account_title"] = account.title
            request.session["setup_token"] = secrets.encrypt(token)
            return RedirectResponse("/setup", status_code=303)

        if account.id != settings.admin_account_id:
            request.session.clear()
            return render(request, "error.html", status_code=403, signed_in=False,
                          title="Not allowed",
                          message=f"Only the owner of {settings.server_name or 'this Plex server'} can use "
                                  f"Syncalist. You signed in as {account.title}.")

        # The admin signed in again: keep their newest token (handles token rotation).
        settings.admin_token_enc = secrets.encrypt(token)
        session.add(settings)
        session.commit()
        request.session["account_id"] = account.id
        request.session["account_title"] = account.title
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # -- setup ---------------------------------------------------------------------------
    def setup_token(request: Request, settings: AppSettings) -> str:
        account_id = request.session.get("account_id")
        if not account_id:
            raise Redirect("/login")
        if settings.admin_account_id not in (None, account_id):
            raise Redirect("/login")
        token = secrets.decrypt(request.session.get("setup_token")) or secrets.decrypt(settings.admin_token_enc)
        if not token:
            raise Redirect("/login")
        return token

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, settings: AppSettings = Depends(app_settings)):
        setup_token(request, settings)
        return render(request, "setup.html", plex_url=settings.plex_url or config.plex_url or "",
                      error=None, account_title=request.session.get("account_title"))

    @app.post("/setup", response_class=HTMLResponse)
    def setup_post(request: Request, plex_url: str = Form(...), session: Session = Depends(db),
                   settings: AppSettings = Depends(app_settings)):
        token = setup_token(request, settings)
        plex_url = plex_url.strip().rstrip("/")
        if not plex_url.startswith(("http://", "https://")):
            plex_url = "http://" + plex_url

        def fail(msg: str):
            return render(request, "setup.html", status_code=400, plex_url=plex_url, error=msg,
                          account_title=request.session.get("account_title"))
        try:
            info = gateway_factory(plex_url, token).server_info()
        except PlexError as e:
            return fail(f"Couldn't connect to Plex at {plex_url}: {e}")
        if not info.owner_is_signed_in_account:
            return fail(f"Your Plex account doesn't own the server “{info.name}”. "
                        "Sign in with the account that owns it.")
        if not info.music_sections:
            return fail(f"“{info.name}” has no music libraries.")

        if settings.machine_id and settings.machine_id != info.machine_id:
            # A different server: anything cached for the old one is invalid.
            for user in session.exec(select(HomeUser)).all():
                session.delete(user)
        settings.plex_url = plex_url
        settings.server_name = info.name
        settings.machine_id = info.machine_id
        settings.admin_account_id = request.session["account_id"]
        settings.admin_title = request.session.get("account_title")
        settings.admin_token_enc = secrets.encrypt(token)
        current = {s.key for s in info.music_sections}
        if settings.music_section_key not in current:
            settings.music_section_key = settings.music_section_title = None
        if len(info.music_sections) == 1:
            settings.music_section_key = info.music_sections[0].key
            settings.music_section_title = info.music_sections[0].title
        session.add(settings)
        session.commit()
        request.session.pop("setup_token", None)
        if not settings.music_section_key:
            return RedirectResponse("/setup/library", status_code=303)
        return _finish_setup(request, session, settings)

    def _finish_setup(request: Request, session: Session, settings: AppSettings):
        try:
            UserService(session, secrets, gateway_for(settings)).refresh()
        except PlexError as e:
            flash(request, f"Set up, but couldn't load Home users yet: {e}", "error")
        flash(request, f"Connected to {settings.server_name}.", "success")
        return RedirectResponse("/", status_code=303)

    @app.get("/setup/library", response_class=HTMLResponse)
    def setup_library(request: Request, settings: AppSettings = Depends(app_settings)):
        setup_token(request, settings)
        try:
            sections = gateway_for(settings).server_info().music_sections
        except PlexError as e:
            return render(request, "error.html", status_code=502, title="Plex unavailable", message=str(e))
        return render(request, "setup_library.html", sections=sections, current=settings.music_section_key)

    @app.post("/setup/library")
    def setup_library_post(request: Request, section_key: str = Form(...), session: Session = Depends(db),
                           settings: AppSettings = Depends(app_settings)):
        setup_token(request, settings)
        sections = {s.key: s for s in gateway_for(settings).server_info().music_sections}
        if section_key not in sections:
            flash(request, "Pick one of the music libraries.", "error")
            return RedirectResponse("/setup/library", status_code=303)
        settings.music_section_key = section_key
        settings.music_section_title = sections[section_key].title
        session.add(settings)
        session.commit()
        return _finish_setup(request, session, settings)

    # -- dashboard (read-only in M1) --------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session = Depends(db),
                  settings: AppSettings = Depends(require_admin)):
        svc = UserService(session, secrets, gateway_for(settings))
        error = None
        try:
            users = svc.all() or svc.refresh()
        except PlexError as e:
            users, error = svc.all(), f"Couldn't load Home users from plex.tv: {e}"
        shares = ShareService(session, svc)
        shared = shares.member_index()
        groups = shares.groups()
        group_members = {g.id: shares.members(g) for g in groups}
        names = {u.id: u.title for u in users}
        rows = []
        for user in users:
            row = {"user": user, "playlists": [], "error": None}
            try:
                row["playlists"] = svc.call_as(user, svc.gateway.playlists)
            except (UserNeedsAttention, PlexError) as e:
                row["error"] = str(e)
            rows.append(row)
        return render(request, "dashboard.html", settings=settings, rows=rows, error=error,
                      shared=shared, groups=groups, group_members=group_members, names=names)

    @app.post("/users/refresh")
    def refresh_users(request: Request, session: Session = Depends(db),
                      settings: AppSettings = Depends(require_admin)):
        try:
            users = UserService(session, secrets, gateway_for(settings)).refresh()
            flash(request, f"Found {len(users)} Home users.", "success")
        except PlexError as e:
            flash(request, f"Couldn't load Home users: {e}", "error")
        return RedirectResponse("/", status_code=303)

    @app.get("/users/{user_id}/playlists/{playlist_key}", response_class=HTMLResponse)
    def playlist(request: Request, user_id: int, playlist_key: int, session: Session = Depends(db),
                 settings: AppSettings = Depends(require_admin)):
        user = session.get(HomeUser, user_id)
        if not user:
            return render(request, "error.html", status_code=404, title="Not found", message="No such Home user.")
        svc = UserService(session, secrets, gateway_for(settings))
        try:
            summary = next((p for p in svc.call_as(user, svc.gateway.playlists) if p.key == playlist_key), None)
            if summary is None:
                return render(request, "error.html", status_code=404, title="Not found",
                              message=f"{user.title} has no music playlist with that id.")
            tracks = svc.call_as(user, lambda tok: svc.gateway.playlist_tracks(tok, playlist_key))
        except (UserNeedsAttention, PlexError) as e:
            return render(request, "error.html", status_code=502, title="Plex error", message=str(e))
        return render(request, "playlist.html", user=user, playlist=summary, tracks=tracks)

    # -- sharing (M2) ------------------------------------------------------------------------------
    def share_service(session: Session, settings: AppSettings) -> ShareService:
        return ShareService(session, UserService(session, secrets, gateway_for(settings)))

    def parse_recipients(form) -> dict[int, Mode]:
        out = {}
        for raw in form.getlist("recipient"):
            uid = int(raw)
            try:
                out[uid] = Mode(form.get(f"mode_{uid}", Mode.NEW.value))
            except ValueError:
                out[uid] = Mode.NEW
        return out

    @app.get("/share", response_class=HTMLResponse)
    def share_page(request: Request, user_id: int, playlist_key: int, session: Session = Depends(db),
                   settings: AppSettings = Depends(require_admin)):
        shares = share_service(session, settings)
        owner = session.get(HomeUser, user_id)
        if owner is None:
            return render(request, "error.html", status_code=404, title="Not found", message="No such Home user.")
        try:
            source = shares._summary(owner, playlist_key)
        except (ShareError, UserNeedsAttention, PlexError) as e:
            return render(request, "error.html", status_code=404, title="Not found", message=str(e))
        if (owner.id, playlist_key) in shares.member_index():
            m = shares.member_index()[(owner.id, playlist_key)]
            return RedirectResponse(f"/groups/{m.group_id}", status_code=303)
        candidates = shares.candidates(source.title, exclude_user_ids={owner.id})
        return render(request, "share.html", owner=owner, source=source, candidates=candidates,
                      action="/share", group=None)

    @app.post("/share")
    async def share_post(request: Request, session: Session = Depends(db),
                         settings: AppSettings = Depends(require_admin)):
        form = await request.form()
        user_id, playlist_key = int(form["user_id"]), int(form["playlist_key"])
        recipients = parse_recipients(form)
        shares = share_service(session, settings)
        owner = session.get(HomeUser, user_id)
        try:
            group = await run_in_threadpool(shares.share, owner, playlist_key, recipients)
        except (ShareError, UserNeedsAttention, PlexError) as e:
            flash(request, str(e), "error")
            return RedirectResponse(f"/share?user_id={user_id}&playlist_key={playlist_key}", status_code=303)
        flash(request, f"Shared “{group.name}”.", "success")
        return RedirectResponse(f"/groups/{group.id}", status_code=303)

    @app.get("/groups/{group_id}", response_class=HTMLResponse)
    def group_page(request: Request, group_id: int, session: Session = Depends(db),
                   settings: AppSettings = Depends(require_admin)):
        group = session.get(ShareGroup, group_id)
        if group is None:
            return render(request, "error.html", status_code=404, title="Not found",
                          message="That shared playlist doesn't exist any more.")
        shares = share_service(session, settings)
        rows = []
        all_skipped: set[int] = set()
        for m in shares.members(group):
            user = session.get(HomeUser, m.user_id)
            row = {"member": m, "user": user, "summary": None, "error": None}
            try:
                row["summary"] = shares.users.call_as(user, lambda t: shares.gateway.playlist(t, m.playlist_key))
                if row["summary"] is None:
                    row["error"] = "Their playlist has been deleted."
            except (UserNeedsAttention, PlexError) as e:
                row["error"] = str(e)
            all_skipped.update(m.skipped)
            rows.append(row)
        skipped_info = {}
        if all_skipped:
            try:
                skipped_info = {t.key: t for t in shares.gateway.tracks_info(sorted(all_skipped))}
            except PlexError:
                pass
        member_ids = {r["user"].id for r in rows}
        candidates = shares.candidates(group.name, exclude_user_ids=member_ids)
        return render(request, "group.html", group=group, rows=rows, skipped_info=skipped_info,
                      candidates=candidates, events=shares.events(group),
                      owner=session.get(HomeUser, group.owner_user_id))

    @app.post("/groups/{group_id}/members")
    async def group_add(request: Request, group_id: int, session: Session = Depends(db),
                        settings: AppSettings = Depends(require_admin)):
        form = await request.form()
        group = session.get(ShareGroup, group_id)
        if group is None:
            return RedirectResponse("/", status_code=303)
        try:
            await run_in_threadpool(share_service(session, settings).add_members, group, parse_recipients(form))
            flash(request, "Updated the shared playlist.", "success")
        except (ShareError, UserNeedsAttention, PlexError) as e:
            flash(request, str(e), "error")
        return RedirectResponse(f"/groups/{group_id}", status_code=303)

    @app.post("/groups/{group_id}/members/{user_id}/remove")
    def group_remove(request: Request, group_id: int, user_id: int, delete_copy: str | None = Form(None),
                     session: Session = Depends(db), settings: AppSettings = Depends(require_admin)):
        group = session.get(ShareGroup, group_id)
        if group is not None:
            try:
                share_service(session, settings).remove_member(group, user_id, delete_playlist=bool(delete_copy))
                flash(request, "Removed.", "success")
            except ShareError as e:
                flash(request, str(e), "error")
        return RedirectResponse(f"/groups/{group_id}", status_code=303)

    @app.post("/groups/{group_id}/stop")
    def group_stop(request: Request, group_id: int, session: Session = Depends(db),
                   settings: AppSettings = Depends(require_admin)):
        group = session.get(ShareGroup, group_id)
        if group is not None:
            name = group.name
            share_service(session, settings).stop_sharing(group)
            flash(request, f"Stopped sharing “{name}”. Everyone keeps their own copy.", "success")
        return RedirectResponse("/", status_code=303)

    @app.get("/backups", response_class=HTMLResponse)
    def backups_page(request: Request, session: Session = Depends(db),
                     settings: AppSettings = Depends(require_admin)):
        shares = share_service(session, settings)
        return render(request, "backups.html", backups=shares.backups(), events=shares.events(limit=100))

    @app.post("/backups/{backup_id}/restore")
    def backup_restore(request: Request, backup_id: int, session: Session = Depends(db),
                       settings: AppSettings = Depends(require_admin)):
        backup = session.get(PlaylistBackup, backup_id)
        if backup is not None:
            try:
                restored = share_service(session, settings).restore(backup)
                flash(request, f"Restored as “{restored.title}” for {backup.user_title}.", "success")
            except ShareError as e:
                flash(request, str(e), "error")
        return RedirectResponse("/backups", status_code=303)

    @app.get("/debug/playlist")
    def debug_playlist(request: Request, user_id: int, playlist_key: int, session: Session = Depends(db),
                       settings: AppSettings = Depends(require_admin)):
        """Read-only diagnostics for one playlist. Contains no tokens."""
        user = session.get(HomeUser, user_id)
        if user is None:
            return JSONResponse({"error": "no such user"}, status_code=404)
        svc = UserService(session, secrets, gateway_for(settings))
        try:
            data = svc.call_as(user, lambda t: svc.gateway.debug_playlist(t, playlist_key))
        except (UserNeedsAttention, PlexError) as e:
            data = {"error": str(e)}
        return JSONResponse({"user": user.title, "user_is_admin": user.is_admin, **data})

    # -- settings ------------------------------------------------------------------------------
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, session: Session = Depends(db),
                      settings: AppSettings = Depends(require_admin)):
        users = UserService(session, secrets, gateway_for(settings)).all()
        return render(request, "settings.html", settings=settings, users=users,
                      min_interval=MIN_INTERVAL, max_interval=MAX_INTERVAL)

    @app.post("/settings")
    def settings_post(request: Request, poll_interval: int = Form(...), session: Session = Depends(db),
                      settings: AppSettings = Depends(require_admin)):
        if not MIN_INTERVAL <= poll_interval <= MAX_INTERVAL:
            flash(request, f"Interval must be between {MIN_INTERVAL} and {MAX_INTERVAL} seconds.", "error")
        else:
            settings.poll_interval = poll_interval
            session.add(settings)
            session.commit()
            flash(request, f"Sync interval set to {poll_interval} seconds.", "success")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/users/{user_id}/pin")
    def set_pin(request: Request, user_id: int, pin: str = Form(...), session: Session = Depends(db),
                settings: AppSettings = Depends(require_admin)):
        user = session.get(HomeUser, user_id)
        if user:
            try:
                UserService(session, secrets, gateway_for(settings)).set_fallback_pin(user, pin.strip())
                flash(request, f"PIN saved for {user.title}.", "success")
            except PlexError as e:
                flash(request, f"That PIN didn't work for {user.title}: {e}", "error")
        return RedirectResponse("/settings", status_code=303)

    return app


def _ago(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:  # plexapi returns naive local times
        value = value.astimezone()
    secs = int((datetime.now(timezone.utc) - value).total_seconds())
    for unit, size in (("year", 31536000), ("month", 2592000), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if secs >= size:
            n = secs // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"
