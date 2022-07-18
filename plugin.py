import functools
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
    PACKAGE_VERSION,
    REQ_CHECK_STATUS,
    REQ_GET_COMPLETIONS,
    REQ_SET_EDITOR_INFO,
)
from .types import (  # CopilotPayloadPanelSolutionDone,
    CopilotPayloadCompletions,
    CopilotPayloadLogMessage,
    CopilotPayloadPanelSolution,
    CopilotPayloadSignInConfirm,
    CopilotPayloadStatusNotification,
)
from .ui import ViewCompletionManager
from .utils import (
    erase_copilot_view_setting,
    first,
    get_copilot_view_setting,
    prepare_completion_request,
    preprocess_completions,
    remove_prefix,
    set_copilot_view_setting,
)


def plugin_loaded() -> None:
    CopilotPlugin.setup()


def plugin_unloaded() -> None:
    CopilotPlugin.cleanup()
    CopilotPlugin.plugin_mapping.clear()


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
    _has_signed_in = False

    def __init__(self, session: "weakref.ref[Session]") -> None:
        super().__init__(session)
        sess = session()
        if sess:
            self.plugin_mapping[sess.window.id()] = self

        # ST persists view setting after getting closed so we have to reset some status
        for window in sublime.windows():
            for view in window.views(include_transient=True):
                erase_copilot_view_setting(view, "is_visible")
                erase_copilot_view_setting(view, "is_waiting_completions")
                erase_copilot_view_setting(view, "is_waiting_panel_completions")

    def on_ready(self, api: ApiWrapperInterface) -> None:
        def on_check_status(result: CopilotPayloadSignInConfirm, failed: bool) -> None:
            self.set_has_signed_in(result.get("status") == "OK")

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
                    "version": PACKAGE_VERSION,
                },
            },
            on_set_editor_info,
        )

    @classmethod
    def minimum_node_version(cls) -> Tuple[int, int, int]:
        # this should be aligned with VSCode's Nodejs version
        return (16, 0, 0)

    @classmethod
    def get_has_signed_in(cls) -> bool:
        return cls._has_signed_in

    @classmethod
    def set_has_signed_in(cls, value: bool) -> None:
        cls._has_signed_in = value
        if value:
            msg = "✈ Copilot has been signed in."
        else:
            msg = "⚠ Copilot has NOT been signed in."
        print("[{}] {}".format(PACKAGE_NAME, msg))
        sublime.status_message(msg)

    @classmethod
    def plugin_from_view(cls, view: sublime.View) -> Optional["CopilotPlugin"]:
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
        panel_id = int(remove_prefix(payload.get("panelId"), "copilot://"))
        target_view = first(sublime.active_window().views(), lambda view: view.id() == panel_id)
        if not target_view:
            return

        panel_completions = get_copilot_view_setting(target_view, "panel_completions", [])
        panel_completions += [payload]

        set_copilot_view_setting(target_view, "panel_completions", panel_completions)

    @notification_handler(NTFY_PANEL_SOLUTION_DONE)
    def _handle_panel_solution_done_notification(self, payload) -> None:
        target_view = None
        for view in sublime.active_window().views():
            temp_view = payload.get("panelId", None)
            if temp_view is None:
                continue
            temp_view = temp_view.replace("copilot://", "")
            if view.id() == int(temp_view):
                target_view = view
                break

        if target_view is None:
            return

        set_copilot_view_setting(target_view, "is_waiting_panel_completions", False)
        ViewCompletionManager(target_view).show_panel_completions()

    @notification_handler(NTFY_STATUS_NOTIFICATION)
    def _handle_status_notification_notification(self, payload: CopilotPayloadStatusNotification) -> None:
        pass

    def request_get_completions(self, view: sublime.View) -> None:
        ViewCompletionManager(view).hide()

        session = self.weaksession()
        sel = view.sel()
        if not (self.get_has_signed_in() and session and len(sel) == 1):
            return

        params = prepare_completion_request(view)
        if params is None:
            return

        cursor = sel[0]
        set_copilot_view_setting(view, "is_waiting_completions", True)
        session.send_request_async(
            Request(REQ_GET_COMPLETIONS, params),
            functools.partial(self._on_get_completions, view, region=cursor.to_tuple()),
        )

    def _on_get_completions(
        self,
        view: sublime.View,
        payload: CopilotPayloadCompletions,
        region: Tuple[int, int],
    ) -> None:
        set_copilot_view_setting(view, "is_waiting_completions", False)

        # re-request completions because the cursor position changed during awaiting Copilot's response
        if view.sel()[0].to_tuple() != region:
            self.request_get_completions(view)
            return

        completions = payload.get("completions")
        if not completions:
            return

        preprocess_completions(view, completions)

        ViewCompletionManager(view).show(completions, 0)
