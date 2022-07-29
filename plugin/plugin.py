import functools
import json
import os
import weakref

import sublime
from LSP.plugin import Request, Session
from LSP.plugin.core.typing import Optional, Tuple
from lsp_utils import ApiWrapperInterface, NpmClientHandler, notification_handler

from .constants import (
    NTFY_LOG_MESSAGE,
    NTFY_PANEL_SOLUTION,
    NTFY_PANEL_SOLUTION_DONE,
    NTFY_STATUS_NOTIFICATION,
    PACKAGE_NAME,
    REQ_CHECK_STATUS,
    REQ_GET_COMPLETIONS,
    REQ_GET_COMPLETIONS_CYCLING,
    REQ_SET_EDITOR_INFO,
)
from .types import (
    AccountStatus,
    CopilotPayloadCompletions,
    CopilotPayloadLogMessage,
    CopilotPayloadPanelSolution,
    CopilotPayloadSignInConfirm,
    CopilotPayloadStatusNotification,
)
from .ui import ViewCompletionManager, ViewPanelCompletionManager
from .utils import (
    all_views,
    prepare_completion_request,
    preprocess_completions,
    preprocess_panel_completions,
    status_message,
)


class CopilotPlugin(NpmClientHandler):
    package_name = PACKAGE_NAME
    server_directory = "language-server"
    server_binary_path = os.path.join(
        server_directory,
        "node_modules",
        "copilot-node-server",
        "copilot",
        "dist",
        "agent.js",
    )

    plugin_mapping = weakref.WeakValueDictionary()  # type: weakref.WeakValueDictionary[int, CopilotPlugin]

    # account status
    _has_signed_in = False
    _is_authorized = False

    def __init__(self, session: "weakref.ref[Session]") -> None:
        super().__init__(session)
        sess = session()
        if sess:
            self.plugin_mapping[sess.window.id()] = self

        # Note that ST persists view settings after ST is closed. If the user closes ST
        # during awaiting Copilot's response, the internal state management will be corrupted.
        # So, we have to reset some status when started.
        for view in all_views():
            ViewCompletionManager(view).reset()
            ViewPanelCompletionManager(view).reset()

    def on_ready(self, api: ApiWrapperInterface) -> None:
        def on_check_status(result: CopilotPayloadSignInConfirm, failed: bool) -> None:
            self.set_account_status(
                signed_in=result["status"] in {"NotAuthorized", "OK"},
                authorized=result["status"] == "OK",
            )

        def on_set_editor_info(result: str, failed: bool) -> None:
            pass

        api.send_request(REQ_CHECK_STATUS, {}, on_check_status)
        api.send_request(
            REQ_SET_EDITOR_INFO,
            {
                "editorInfo": {
                    "name": "Sublime Text",
                    "version": sublime.version(),
                },
                "editorPluginInfo": {
                    "name": PACKAGE_NAME,
                    "version": self.version(),
                },
            },
            on_set_editor_info,
        )

    @staticmethod
    def version() -> str:
        """Return this plugin's version. If it's not installed by Package Control, return `"unknown"`."""
        try:
            return json.loads(sublime.load_resource("Packages/{}/package-metadata.json".format(PACKAGE_NAME)))[
                "version"
            ]
        except Exception:
            return "unknown"

    @classmethod
    def minimum_node_version(cls) -> Tuple[int, int, int]:
        # this should be aligned with VSCode's Nodejs version
        return (16, 0, 0)

    @classmethod
    def get_account_status(cls) -> AccountStatus:
        """Return `(has_signed_in, is_authorized)`."""
        return AccountStatus(cls._has_signed_in, cls._is_authorized)

    @classmethod
    def set_account_status(
        cls,
        *,
        signed_in: Optional[bool] = None,
        authorized: Optional[bool] = None,
        quiet: bool = False
        # format delimiter
    ) -> None:
        if signed_in is not None:
            cls._has_signed_in = signed_in
        if authorized is not None:
            cls._is_authorized = authorized

        if not quiet:
            if not cls._has_signed_in:
                icon, msg = "❌", "has NOT been signed in."
            elif not cls._is_authorized:
                icon, msg = "⚠", "has signed in but not authorized."
            else:
                icon, msg = "✈", "has been signed in and authorized."
            status_message(msg, icon_=icon, console_=True)

    @classmethod
    def from_view(cls, view: sublime.View) -> Optional["CopilotPlugin"]:
        window = view.window()
        if not window:
            return None
        self = cls.plugin_mapping.get(window.id())
        if not (self and self.is_valid_for_view(view)):
            return None
        return self

    def is_valid_for_view(self, view: sublime.View) -> bool:
        session = self.weaksession()
        return bool(session and session.session_view_for_view_async(view))

    @notification_handler(NTFY_LOG_MESSAGE)
    def _handle_log_message_notification(self, payload: CopilotPayloadLogMessage) -> None:
        pass

    @notification_handler(NTFY_PANEL_SOLUTION)
    def _handle_panel_solution_notification(self, payload: CopilotPayloadPanelSolution) -> None:
        view = ViewPanelCompletionManager.find_view_by_panel_id(payload["panelId"])
        if not view:
            return

        preprocess_panel_completions(view, [payload])

        completion_manager = ViewPanelCompletionManager(view)
        completion_manager.append_completion(payload)
        completion_manager.update()

    @notification_handler(NTFY_PANEL_SOLUTION_DONE)
    def _handle_panel_solution_done_notification(self, payload) -> None:
        view = ViewPanelCompletionManager.find_view_by_panel_id(payload["panelId"])
        if not view:
            return

        completion_manager = ViewPanelCompletionManager(view)
        completion_manager.is_waiting = False
        completion_manager.update()

    @notification_handler(NTFY_STATUS_NOTIFICATION)
    def _handle_status_notification_notification(self, payload: CopilotPayloadStatusNotification) -> None:
        pass

    def request_get_completions(self, view: sublime.View) -> None:
        self._request_completions(view=view, request=REQ_GET_COMPLETIONS)
        self.request_get_completions_cycling(view=view)

    def request_get_completions_cycling(self, view: sublime.View) -> None:
        self._request_completions(view=view, request=REQ_GET_COMPLETIONS_CYCLING)

    def _request_completions(self, view: sublime.View, request: str) -> None:
        completion_manager = ViewCompletionManager(view)
        completion_manager.hide()

        has_signed_in, is_authorized = self.get_account_status()
        session = self.weaksession()
        sel = view.sel()
        if not (has_signed_in and is_authorized and session and len(sel) == 1):
            return

        params = prepare_completion_request(view)
        if not params:
            return

        completion_manager.is_waiting = True
        session.send_request_async(
            Request(request, params),
            functools.partial(self._on_get_completions, view, region=sel[0].to_tuple()),
        )

    def _on_get_completions(
        self,
        view: sublime.View,
        payload: CopilotPayloadCompletions,
        region: Tuple[int, int],
    ) -> None:
        completion_manager = ViewCompletionManager(view)
        completion_manager.is_waiting = False

        # re-request completions because the cursor position changed during awaiting Copilot's response
        if view.sel()[0].to_tuple() != region:
            self.request_get_completions(view)
            return

        completions = payload["completions"]
        if not completions:
            return

        preprocess_completions(view, completions)
        completion_manager.show(completions, 0)
