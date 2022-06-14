import logging

import sublime
import sublime_plugin
import threading

from sublime import Region, View


class NamespacedName:
    """Parses a string like 'wd-wsdl:sometype' and returns ('wd-wsdl', 'sometype')"""

    def __init__(self, namespaced_name: str):
        parts = namespaced_name.split(':')
        if len(parts) < 2:
            Logger.log_warn("Expected a namespaced name, but got '{}'.".format(namespaced_name))
        else:
            self.ns = parts[0]
            self.name = parts[1]

    def __repr__(self):
        return "{}:{}".format(self.ns, self.name)


class HashableRegion:
    # Because Region is not already hashable :(

    def __init__(self, region: Region):
        self.region = region

    def __hash__(self):
        return hash((self.region.a, self.region.b, self.region.xpos))

    def __str__(self):
        return self.region.__str__()

    def __repr__(self):
        return self.region.__repr__()


class LogLevel:
    """Primitive enum"""
    Off = 0
    Error = 1
    Warn = 2
    Info = 3
    Debug = 4

    @staticmethod
    def string_to_log_level(level: str) -> int:
        """Try to parse the given string into a LogLevel, fallback to Info."""
        if level == "Off":
            return LogLevel.Off
        elif level == "Error":
            return LogLevel.Error
        elif level == "Warn":
            return LogLevel.Warn
        elif level == "Info":
            return LogLevel.Info
        elif level == "Debug":
            return LogLevel.Debug
        else:
            return LogLevel.Info


class ClickableHighlighter(sublime_plugin.EventListener):
    """Highlights clickable things (for what counts as clickable, see CLICKABLE_REGEX)"""

    NAMESPACE_REGEX = 'xmlns:([^=])+="([^"]+)"'
    CLICKABLE_REGEX = '(type|ref|base|element|message|binding)="([^"]+)"'
    DEFAULT_MAX_CLICKABLES = 1000
    SETTINGS_FILENAME = 'ClickableWsdl.sublime-settings'

    clickables_for_view = {}
    scopes_for_view = {}
    target_namespace_scopes_for_view = {}  # Dict of {View.id(): [Namespace_Scope]}
    ignored_views = []
    highlight_semaphore = threading.Semaphore()

    def on_activated(self, view: View):
        if is_xml_file(view):
            self.update_clickable_highlights(view)

    # Blocking handlers for ST2
    def on_load(self, view: View):
        if sublime.version() < '3000' and is_xml_file(view):
            self.update_clickable_highlights(view)

    def on_modified(self, view: View):
        if sublime.version() < '3000' and is_xml_file(view):
            self.update_clickable_highlights(view)

    # Async listeners for ST3
    def on_load_async(self, view: View):
        if is_xml_file(view):
            self.update_clickable_highlights_async(view)

    def on_modified_async(self, view: View):
        if is_xml_file(view):
            self.update_clickable_highlights_async(view)

    def on_close(self, view: View):
        for map in [self.clickables_for_view, self.scopes_for_view, self.ignored_views]:
            if view.id() in map:
                del map[view.id()]

    def update_clickable_highlights(self, view: View):
        """The logic entry point. Find all clickables in view, store and highlight them."""

        if view.id() not in ClickableHighlighter.ignored_views:
            # Find targetNamespaces of this doc (e.g. targetNamespace="urn:com.workday/bsvc")
            # We could have multiple, like one for the top-level WSDL and another one for just the xsd:schema.
            # For now, assume that there are not duplicates across namespaces, since we will only navigate to the first
            # one found (TODO future enhancement?)
            # This is so that we know if we should expect to find the referenced elements in this file or if they're
            # from another namespace (e.g. xsd:string isn't navigable within a WSDL because the xsd-namespace types are
            # defined elsewhere (e.g. the namespace is declared as `xmlns:xsd="http://www.w3.org/2001/XMLSchema"`).

            # Find all target namespaces (so we know what is navigable within this View (file))
            target_ns_regions = view.find_all('targetNamespace="([^"]+)"')
            # To determine the end of the targetNamespace scope, look for the next closing xml tag of the same name.
            # First, figure out what the xml tag is for each found target ns by looking backwards for the closest "<".
            target_ns_region_element_names = [
                (target_ns_region, get_element_name_containing_region(view, target_ns_region)) for
                target_ns_region in target_ns_regions
            ]
            Logger.log_debug("target_ns_region_element_names: {}".format(target_ns_region_element_names))
            # Then, find the next closing tag with the same name
            # TODO/WARNING: this does not work for self-closing tags like <xsd:schema ... />!
            # TODO/WARNING: this also does not work for nested same-named elements (hopefully uncommon?)
            target_namespace_scopes = [
                NamespaceScope(
                    KeyValue(view.substr(target_ns_region)).value,
                    target_ns_region,
                    get_closing_element_tag_region(view, target_ns_region, namespaced_name)
                ) for (target_ns_region, namespaced_name) in target_ns_region_element_names
            ]
            Logger.log_debug("target_namespace_scopes: {}".format(target_namespace_scopes))
            ClickableHighlighter.target_namespace_scopes_for_view.update({view.id(): target_namespace_scopes})

            # Find all namespaces (e.g. 'xmlns:nyw="urn:com.netyourwork/aod"')
            namespace_regions = view.find_all(ClickableHighlighter.NAMESPACE_REGEX)
            namespace_region_and_strings = {(HashableRegion(namespace_region), view.substr(namespace_region)) for
                                            namespace_region in namespace_regions}
            Logger.log_debug("namespace_region_and_strings: {}".format(namespace_region_and_strings))
            namespace_regions_and_ns_with_xmlns_prefix = {(namespace_region, KeyValue(namespace_string)) for
                                                          (namespace_region, namespace_string) in
                                                          namespace_region_and_strings}
            Logger.log_debug(
                "namespace_regions_and_ns_with_xmlns_prefix: {}".format(namespace_regions_and_ns_with_xmlns_prefix))
            namespaces = {
                NamespacedName(namespace_key_value.key).name: (namespace_key_value.value, namespace_region)
                for
                (namespace_region, namespace_key_value) in namespace_regions_and_ns_with_xmlns_prefix
                if is_valid_target_namespace(namespace_key_value, target_namespace_scopes)
            }
            Logger.log_debug("namespaces: {}".format(namespaces))

            # Find all clickable things
            reference_regions = view.find_all(ClickableHighlighter.CLICKABLE_REGEX)
            Logger.log_debug("Reference regions: {}".format(
                [view.substr(r) for r in reference_regions]))  # e.g. 'type="wd:Validation_FaultType"'
            reference_ns_alias_and_regions = [(NamespacedName(KeyValue(view.substr(r)).value).ns, r) for r in
                                              reference_regions]
            valid_reference_regions = [r for (ns_alias, r) in reference_ns_alias_and_regions if ns_alias in namespaces]
            Logger.log_debug("Valid reference regions: {}".format(valid_reference_regions))
            clickable_regions = [get_clickable_region_from_reference_region(view, r) for r in
                                 valid_reference_regions]
            Logger.log_debug("Clickable regions: {}".format([view.substr(r) for r in clickable_regions]))

            settings = sublime.load_settings(ClickableHighlighter.SETTINGS_FILENAME)
            max_clickable_limit = settings.get('max_clickable_limit', ClickableHighlighter.DEFAULT_MAX_CLICKABLES)
            Logger.log_debug("settings.max_clickable_limit: {}".format(settings.get('max_clickable_limit')))

            # Avoid slowdowns for views with too many clickables
            Logger.log_debug("num clickable_regions: {}".format(len(clickable_regions)))
            if len(clickable_regions) > max_clickable_limit:
                Logger.log_info("ignoring view with %u clickables" % len(clickable_regions))
                sublime.status_message("Too many clickables to highlight; ignoring file.")
                ClickableHighlighter.ignored_views.append(view.id())
            else:
                ClickableHighlighter.clickables_for_view[view.id()] = clickable_regions

                should_highlight_clickables = sublime.load_settings(ClickableHighlighter.SETTINGS_FILENAME).get(
                    'highlight_clickables', True)
                if should_highlight_clickables:
                    self.highlight_clickables(view, clickable_regions)

    def update_clickable_highlights_async(self, view: View):
        """Same as update_clickable_highlights, but avoids race conditions with a semaphore."""

        ClickableHighlighter.highlight_semaphore.acquire()
        try:
            self.update_clickable_highlights(view)
        finally:
            ClickableHighlighter.highlight_semaphore.release()

    def highlight_clickables(self, view, clickable_regions):
        """Creates a set of regions from the intersection of clickables and scopes, underlines all of them."""

        # We need separate regions for each lexical scope for ST to use a proper color for the underline
        scope_map = {}
        for clickable_region in clickable_regions:
            scope_name = view.scope_name(clickable_region.begin())
            scope_map.setdefault(scope_name, []).append(clickable_region)

        for scope_name in scope_map:
            self.underline_regions(view, scope_name, scope_map[scope_name])

        self.update_view_scopes(view, scope_map.keys())

    @staticmethod
    def underline_regions(view: View, scope_name: str, regions: [Region]):
        """Apply underlining with provided scope name to provided regions.
        Uses the empty region underline hack for Sublime Text 2 and native underlining for Sublime Text 3."""

        if sublime.version() >= '3019':
            # in Sublime Text 3, the regions are just underlined
            view.add_regions(
                u'clickables ' + scope_name,
                regions,
                scope_name,
                flags=sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.DRAW_SOLID_UNDERLINE)
        else:
            # in Sublime Text 2, the 'empty region underline' hack is used
            char_regions = [sublime.Region(pos, pos) for region in regions for pos in
                            range(region.begin(), region.end())]
            view.add_regions(
                u'clickables ' + scope_name,
                char_regions,
                scope_name,
                sublime.DRAW_EMPTY_AS_OVERWRITE
            )

    def update_view_scopes(self, view, new_scopes):
        """Store new set of underlined scopes for view.
        Erase underlining from scopes that were used but are not anymore."""

        old_scopes = self.scopes_for_view.get(view.id(), None)
        if old_scopes:
            unused_scopes = set(old_scopes) - set(new_scopes)
            for unused_scope_name in unused_scopes:
                view.erase_regions(u'clickables ' + unused_scope_name)

        self.scopes_for_view[view.id()] = new_scopes


class NavigateToLinkUnderCursorCommand(sublime_plugin.TextCommand):
    """Finds the definition region for a given clickable string"""

    def is_enabled(self):
        return is_xml_file(self.view)

    def find_name_region(self, name: str, start_point: int) -> Region:
        return self.view.find("name=\"{}\"".format(name), start_point)

    def find_definition_region_for_clickable(self, clickable_region) -> Region:
        """Find the target definition Region for the given clickable Region (i.e. where to navigate to)"""

        clickable_text = self.view.substr(clickable_region)
        namespaced_name = NamespacedName(clickable_text)
        Logger.log_debug(
            "Looking for ({}, '{}') in target scopes: {}".format(clickable_region, self.view.substr(clickable_region),
                                                                 ClickableHighlighter.target_namespace_scopes_for_view[
                                                                     self.view.id()]))
        target_ns_scopes_containing_clickable = [
            s for s in ClickableHighlighter.target_namespace_scopes_for_view[self.view.id()]
            if s.contains_region(clickable_region)
        ]

        search_start_point = None
        if len(target_ns_scopes_containing_clickable) == 1:
            search_start_point = target_ns_scopes_containing_clickable[0].start_region.end()
        elif len(target_ns_scopes_containing_clickable) > 1:
            closest_ns = find_closest_enclosing_scope(clickable_region, target_ns_scopes_containing_clickable)
            Logger.log_debug("Closest enclosing NamespaceScope for ({} '{}') is {}".format(clickable_region,
                                                                                           self.view.substr(
                                                                                               clickable_region),
                                                                                           closest_ns))

            # Search for clickable starting at the closest matching target namespace region
            # (this should find it within the target namespace scope, but it may return a result after/outside the
            # target namespace scope; I think that's an acceptable edge case).
            search_start_point = closest_ns.start_region.end()
        else:
            # If no target_ns_scopes matched, just try to find the clickable anywhere.
            Logger.log_debug("Failed to find '{}' in a target namespace scope, falling back to generic search".format(
                namespaced_name.name))
            search_start_point = -1

        def_region = self.find_name_region(namespaced_name.name, search_start_point)

        if def_region.intersects(clickable_region):
            # We found the clickable_region, not the definition; try searching one more time, but this time starting
            # at the clickable_region's end.
            def_region = self.find_name_region(namespaced_name.name, def_region.end())

        Logger.log_debug("found def_region: ({} '{}')".format(def_region, self.view.substr(def_region)))
        return def_region

    def navigate_to_region(self, region) -> None:
        """Navigates to a region and changes the selection to the region."""

        Logger.log_debug("navigating to region {}".format(region))
        self.view.show(region, True, False, True)
        selection = self.view.sel()
        selection.clear()
        selection.add(region)

    def run(self, edit) -> None:
        if self.view.id() in ClickableHighlighter.clickables_for_view:
            selection = self.view.sel()[0]
            Logger.log_debug("current selection = {}".format(selection))
            if selection.empty():
                # expand selection to include the clickable if there is one here
                Logger.log_debug(
                    "clickables_for_view: {}".format(ClickableHighlighter.clickables_for_view[self.view.id()]))
                selection = next(
                    (clickable_region for clickable_region in ClickableHighlighter.clickables_for_view[self.view.id()]
                     if
                     clickable_region.contains(selection)), None
                )
                if not selection:
                    # Do nothing if there is no clickable here
                    sublime.status_message("No clickable here")
                    return
                Logger.log_debug(
                    "navigate to definition of selection: ({} '{}')".format(selection, self.view.substr(selection)))

            destination_region = self.find_definition_region_for_clickable(selection)
            # Only navigate if we found something
            if destination_region.begin() > -1:
                self.navigate_to_region(destination_region)
            else:
                sublime.status_message("Failed to find definition of {}".format(self.view.substr(selection)))


class NamespaceScope:
    """A target namespace and its start/end Regions."""

    def __init__(self, ns: str, start_region: Region, end_region: Region):
        self.ns = ns
        self.start_region = start_region
        self.end_region = end_region

    def contains_region(self, region: Region) -> bool:
        """Whether this namespace scope covers the given region."""
        return self.start_region.cover(self.end_region).contains(region)

    def __repr__(self):
        return "NamespaceScope({}, {}, {})".format(self.ns, self.start_region, self.end_region)

    def __hash__(self):
        return hash((self.ns, HashableRegion(self.start_region), HashableRegion(self.end_region)))


class KeyValue:
    """Parses a string like 'key="value"' and returns ('key', 'value')"""

    def __init__(self, key_value_str: str):
        parts = key_value_str.split('=');
        self.key = parts[0]
        self.value = parts[1].replace('"', '')

    def __repr__(self):
        return "{}={}".format(self.key, self.value)


def is_xml_file(view: View) -> bool:
    """Returns True if this view is an XML file."""
    supported_syntaxes = ["XML", "HTML", "XSL"]
    supported = view.syntax().name in supported_syntaxes
    if not supported:
        Logger.log_debug("Syntax '{}' is not supported".format(view.syntax().name))
    return supported


def get_clickable_region_from_reference_region(view: View, reference_region: Region) -> Region:
    """Shrink the region so we don't have the full 'type="wd:Validation_FaultType"' but instead just
    'wd:Validation_FaultType'"""

    reference_region_string = view.substr(reference_region)
    new_begin = reference_region.begin() + 1 + reference_region_string.index('"')
    new_end = reference_region.end() - 1
    clickable_region = sublime.Region(new_begin, new_end)

    return clickable_region


def get_element_name_containing_region(view: View, region: Region): # -> Optional[NamespacedName]
    """Gets the namespaced name of the xml element containing the given region"""

    next_point = view.find_by_class(region.begin(), False,
                                    sublime.CLASS_WORD_START | sublime.CLASS_PUNCTUATION_START)
    next_space = view.find('\s', next_point)
    full_word_region = Region(next_point, next_space.begin())
    full_word_region_str = view.substr(full_word_region)
    if full_word_region_str.startswith('<'):
        return NamespacedName(full_word_region_str[1:])
    elif next_point == 0:
        sublime.status_message("Invalid XML. ClickableWSDL links will not work.")
        Logger.log_error("Cannot find the element containing the region {}".format(view.substr(region)))
        return None
    else:
        return get_element_name_containing_region(view, full_word_region)


def get_closing_element_tag_region(view: View, start_region: Region, ns: NamespacedName) -> Region:
    """Gets the region for the closing tag for the given namespace starting with the startRegion"""

    search_for = "/" + ns.__str__()
    found_region = view.find(search_for, start_region.end())
    if found_region.a == -1 and found_region.b == -1:
        sublime.status_message("Invalid XML. ClickableWSDL links will not work.")
        Logger.log_error("Cannot find the closing tag for element '{}' {}".format(ns, start_region))
        return None
    else:
        return found_region


def is_valid_target_namespace(namespace: KeyValue, target_ns_scopes: [NamespaceScope]) -> bool:
    """Returns whether the given namespace is valid as a target namespace."""

    for target_ns_scope in target_ns_scopes:
        if namespace.value == target_ns_scope.ns:
            return True

    return False


def find_closest_enclosing_scope(clickable_region: Region,
                                 target_ns_scopes_containing_clickable: [NamespaceScope]) -> NamespaceScope:
    """Find which target NamespaceScope is the closest enclosing scope for the clickable_region"""

    clickable_begin = clickable_region.begin()

    closest_ns = target_ns_scopes_containing_clickable[0]  # start with the first
    closest_ns_dist_to_clickable_begin = abs(closest_ns.start_region.begin() - clickable_begin)

    for ns in target_ns_scopes_containing_clickable[1:]:
        ns_dist_to_clickable_begin = abs(clickable_begin - ns.start_region.begin())
        # note: two different target namespaces shouldn't have the same region
        if ns_dist_to_clickable_begin < closest_ns_dist_to_clickable_begin:
            closest_ns = ns
            closest_ns_dist_to_clickable_begin = ns_dist_to_clickable_begin

    return closest_ns


class Logger:
    """Primitive logger"""

    log_level = LogLevel.string_to_log_level(
        sublime.load_settings(ClickableHighlighter.SETTINGS_FILENAME).get('log_level', "Info"))
    print("log_level is set to '{}'".format(log_level))

    @classmethod
    def log_error(cls, message: str):
        if cls.log_level <= LogLevel.Error:
            print("[ERROR ClickableWsdl]: {}".format(message))

    @classmethod
    def log_warn(cls, message: str):
        if cls.log_level <= LogLevel.Warn:
            print("[WARNING ClickableWsdl]: {}".format(message))

    @classmethod
    def log_info(cls, message: str):
        if cls.log_level <= LogLevel.Info:
            print("[INFO ClickableWsdl]: {}".format(message))

    @classmethod
    def log_debug(cls, message: str):
        if cls.log_level <= LogLevel.Debug:
            print("[DEBUG ClickableWsdl]: {}".format(message))
