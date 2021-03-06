import textwrap

import mdpopups
import sublime
from LSP.plugin.core.typing import List, Optional

from ..types import CopilotPayloadCompletion
from ..utils import (
    clamp,
    fix_completion_syntax_highlight,
    get_copilot_view_setting,
    get_view_language_id,
    reformat,
    set_copilot_view_setting,
)


class ViewCompletionManager:
    # ------------- #
    # view settings #
    # ------------- #

    @property
    def is_visible(self) -> bool:
        """Whether Copilot's completion popup is visible."""
        return get_copilot_view_setting(self.view, "is_visible", False)

    @is_visible.setter
    def is_visible(self, value: bool) -> None:
        set_copilot_view_setting(self.view, "is_visible", value)

    @property
    def is_waiting(self) -> bool:
        """Whether the view is waiting for Copilot's completion response."""
        return get_copilot_view_setting(self.view, "is_waiting_completion", False)

    @is_waiting.setter
    def is_waiting(self, value: bool) -> None:
        set_copilot_view_setting(self.view, "is_waiting_completion", value)

    @property
    def completions(self) -> List[CopilotPayloadCompletion]:
        """All `completions` in the view. Note that this is a copy."""
        return get_copilot_view_setting(self.view, "completions", [])

    @completions.setter
    def completions(self, value: List[CopilotPayloadCompletion]) -> None:
        set_copilot_view_setting(self.view, "completions", value)

    @property
    def completion_index(self) -> int:
        """The index of the current chosen completion."""
        return get_copilot_view_setting(self.view, "completion_index", 0)

    @completion_index.setter
    def completion_index(self, value: int) -> None:
        set_copilot_view_setting(
            self.view,
            "completion_index",
            self._tidy_completion_index(value),
        )

    # -------------- #
    # normal methods #
    # -------------- #

    def __init__(self, view: sublime.View) -> None:
        self.view = view

    def reset(self) -> None:
        self.is_visible = False
        self.is_waiting = False

    @property
    def current_completion(self) -> Optional[CopilotPayloadCompletion]:
        """The current chosen `completion`."""
        return self.completions[self.completion_index] if self.completions else None

    def show_previous_completion(self) -> None:
        """Show the previous completion."""
        self.show(completion_index=self.completion_index - 1)

    def show_next_completion(self) -> None:
        """Show the next completion."""
        self.show(completion_index=self.completion_index + 1)

    def hide(self) -> None:
        """Hide Copilot's completion popup."""
        # prevent from hiding other plugin's popup
        if self.is_visible:
            _PopupCompletion.hide(self.view)

        self.is_visible = False

    def show(
        self,
        completions: Optional[List[CopilotPayloadCompletion]] = None,
        completion_index: Optional[int] = None,
    ) -> None:
        """Show Copilot's completion popup."""
        if completions is not None:
            self.completions = completions
        if completion_index is not None:
            self.completion_index = completion_index

        completion = self.current_completion
        if not completion:
            return

        # the text after completion is the same
        current_line = self.view.line(completion["point"])
        if completion["text"] == self.view.substr(current_line):
            return

        _PopupCompletion(self.view).show()

        self.is_visible = True

    def _tidy_completion_index(self, index: int) -> int:
        """Revise `completion_index` to a valid value, or `0` if `self.completions` is empty."""
        completions_cnt = len(self.completions)
        if not completions_cnt:
            return 0

        # clamp if it's out-of-bounds or treat it as cyclic?
        if self.view.settings().get("auto_complete_cycle", False):
            return index % completions_cnt
        return clamp(index, 0, completions_cnt - 1)


class _PopupCompletion:
    CSS_CLASS_NAME = "copilot-completion-popup"
    CSS = """
    html {{
        --copilot-accept-foreground: var(--foreground);
        --copilot-accept-background: var(--background);
        --copilot-accept-border: var(--greenish);
        --copilot-reject-foreground: var(--foreground);
        --copilot-reject-background: var(--background);
        --copilot-reject-border: var(--yellowish);
    }}

    .{class_name} {{
        margin: 1rem 0.5rem 0 0.5rem;
    }}

    .{class_name} .header {{
        display: block;
        margin-bottom: 1rem;
    }}

    .{class_name} a {{
        border-radius: 3px;
        border-style: solid;
        border-width: 1px;
        display: inline;
        padding: 5px;
        text-decoration: none;
    }}

    .{class_name} a.accept {{
        background: var(--copilot-accept-background);
        border-color: var(--copilot-accept-border);
        color: var(--copilot-accept-foreground);
    }}

    .{class_name} a.accept i {{
        color: var(--copilot-accept-border);
    }}

    .{class_name} a.reject {{
        background: var(--copilot-reject-background);
        border-color: var(--copilot-reject-border);
        color: var(--copilot-reject-foreground);
    }}

    .{class_name} a.reject i {{
        color: var(--copilot-reject-border);
    }}

    .{class_name} a.prev {{
        border-top-right-radius: 0;
        border-bottom-right-radius: 0;
        border-right-width: 0;
        padding-left: 8px;
        padding-right: 8px;
    }}

    .{class_name} a.next {{
        border-top-left-radius: 0;
        border-bottom-left-radius: 0;
        border-left-width: 0;
        padding-left: 8px;
        padding-right: 8px;
    }}

    .{class_name} a.panel {{
        padding-left: 8px;
        padding-right: 8px;
    }}
    """.format(
        class_name=CSS_CLASS_NAME
    )
    # We use many backticks to denote a fenced code block because if we are writing in Markdown,
    # Copilot may suggest 3 backticks for a fenced code block and that can break our templating.
    COMPLETION_TEMPLATE = reformat(
        """
        <div class="header">{header_items}</div>
        ``````{lang}
        {code}
        ``````
        """
    )

    def __init__(self, view: sublime.View) -> None:
        self.view = view
        self.completion_manager = ViewCompletionManager(view)

    @property
    def popup_content(self) -> str:
        self.completion = self.completion_manager.current_completion
        assert self.completion  # our code flow guarantees this
        return self.COMPLETION_TEMPLATE.format(
            header_items=" &nbsp;".join(self.popup_header_items),
            lang=get_view_language_id(self.view, self.completion["point"]),
            code=self.popup_code,
        )

    @property
    def popup_header_items(self) -> List[str]:
        completions_cnt = len(self.completion_manager.completions)
        header_items = [
            '<a class="accept" title="Accept Completion" href="subl:copilot_accept_completion"><i>???</i> Accept</a>',
            '<a class="reject" title="Reject Completion" href="subl:copilot_reject_completion"><i>??</i> Reject</a>',
        ]
        if completions_cnt > 1:
            header_items.append(
                '<a class="prev" title="Previous Completion" href="subl:copilot_previous_completion">???</a>'
                + '<a class="next" title="Next Completion" href="subl:copilot_next_completion">???</a>'
            )
            header_items.append(
                "({completion_index_1} of {completions_cnt})".format(
                    completion_index_1=self.completion_manager.completion_index + 1,  # 1-based index
                    completions_cnt=completions_cnt,
                )
            )
        header_items.append(
            '<a class="panel" href="subl:copilot_get_panel_completions" title="Open Panel Completions">???</a>'
        )
        return header_items

    @property
    def popup_code(self) -> str:
        self.completion = self.completion_manager.current_completion
        assert self.completion  # our code flow guarantees this
        return fix_completion_syntax_highlight(
            self.view,
            self.completion["point"],
            textwrap.dedent(self.completion["text"]),
        )

    def show(self) -> None:
        mdpopups.show_popup(
            view=self.view,
            content=self.popup_content,
            md=True,
            css=self.CSS,
            layout=sublime.LAYOUT_INLINE,
            flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
            max_width=640,
            wrapper_class=self.CSS_CLASS_NAME,
        )

    @staticmethod
    def hide(view: sublime.View) -> None:
        mdpopups.hide_popup(view)
