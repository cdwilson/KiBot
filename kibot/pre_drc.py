# -*- coding: utf-8 -*-
# Copyright (c) 2020-2024 Salvador E. Tropea
# Copyright (c) 2020-2024 Instituto Nacional de Tecnología Industrial
# License: AGPL-3.0
# Project: KiBot (formerly KiPlot)
# https://gitlab.com/kicad/code/kicad/-/blob/master/resources/schemas/drc.v1.json?ref_type=heads
import csv
import io
import os
from .pre_any_xrc import DRCOptions, XRC, UNITS_2_KICAD
from .macros import macros, document, pre_class  # noqa: F401
from .gs import GS
from .misc import W_DRC, W_FILXRC
from .log import get_logger
logger = get_logger(__name__)
JSON_SECTIONS = ('violations', 'unconnected_items', 'schematic_parity')
SECTION_HUMAN = {'schematic_parity': 'Schematic parity', 'unconnected_items': 'Unconnected items', 'violations': 'Violations'}
SECTION_RPT = {'schematic_parity': 'Footprint errors', 'unconnected_items': 'unconnected pads', 'violations': 'DRC violations'}


@pre_class
class DRC(XRC):  # noqa: F821
    """ [boolean=false|dict] Runs the DRC (Distance Rules Check). To ensure we have a valid PCB.
        This is a replacement for the *run_drc* preflight that needs KiCad 8 or newer.
        GUI exclusions and schematic parity are supported """
    def __init__(self, name, value):
        super().__init__(name, value, DRCOptions)
        self._pcb_related = True
        self._expand_id = 'drc'

    @classmethod
    def get_doc(cls):
        return cls.__doc__, DRCOptions

    def apply_filters(self, data):
        filters = []
        if self._filters:
            filters += self._filters
        if GS.filters:
            logger.error(GS.filters)
            filters += GS.filters
            logger.warning(W_FILXRC+'Using filters from the `filters` preflight, move them to `drc`')
        self.c_err = self.c_warn = self.c_tot = 0
        self.c_err_excl = self.c_warn_excl = self.c_tot_excl = 0
        ign_unc = self._ignore_unconnected or BasePreFlight.get_option('ignore_unconnected')  # noqa: F821
        for section in JSON_SECTIONS:
            ignore = ign_unc and section == 'unconnected_items'
            for violation in data.get(section, []):
                severity = violation.get('severity', 'error')
                type = violation.get('type', '')
                # Collect the text using a layout equivalent to the report
                txt = violation.get('description', '')+'\n'
                for item in violation.get('items', []):
                    txt += self.get_item_txt(item)
                # Check if we want to exclude it
                excluded = violation.get('excluded')
                if ignore:
                    # Ignore unconnected enabled
                    if not excluded:
                        violation['excluded'] = True
                        violation['excluded_by_kibot'] = True
                        excluded = True
                else:
                    # Check if any filter matches this violation
                    for f in filters:
                        if type == f.error and f.regex.search(txt):
                            change_to = f.change_to if hasattr(f, 'change_to') else 'ignore'
                            if change_to == 'ignore':
                                if not excluded:
                                    violation['excluded'] = True
                                    violation['excluded_by_kibot'] = True
                                    excluded = True
                            else:
                                violation['excluded'] = False
                                violation['modified_by_kibot'] = True
                                severity = violation['severity'] = change_to
                                excluded = False
                            break
                if excluded:
                    if severity == 'error':
                        self.c_err_excl += 1
                    else:
                        self.c_warn_excl += 1
                    self.c_tot_excl += 1
                else:
                    if severity == 'error':
                        self.c_err += 1
                    else:
                        self.c_warn += 1
                    self.c_tot += 1

    def create_csv(self, data):
        f = io.StringIO()
        writer = csv.writer(f, delimiter=',', lineterminator="\n", quoting=csv.QUOTE_ALL)
        writer.writerow(['Check', 'Severity', 'Excluded', 'Type', 'Description', 'Details'])
        for section in JSON_SECTIONS:
            violations = data.get(section, [])
            name = SECTION_HUMAN[section]
            for violation in violations:
                severity = violation.get('severity', 'error')
                excluded = violation.get('excluded', False)
                type = violation.get('type', '')
                description = violation.get('description', '')
                details = ''
                for item in violation.get('items', []):
                    details += self.get_item_txt(item, indent=0, sep=';')
                writer.writerow([name, severity, excluded, type, description, details[:-1]])
        return f.getvalue()

    def create_html(self, data):
        # HTML Head
        html = self.create_html_top(data)
        # Generate the content
        for section in JSON_SECTIONS:
            violations = data.get(section, [])
            if not violations:
                continue
            name = SECTION_HUMAN[section]
            html += f'<p class="subtitle">{name}</p>\n'
            html += self.create_html_violations(violations)
        html += self.create_html_bottom()
        return html

    def create_txt(self, data):
        dt = data.get('date', '??')
        rpt = f'** Drc report for {os.path.basename(GS.pcb_file)} **\n'
        rpt += f"** Created on {dt} **\n\n"
        for s in JSON_SECTIONS:
            violations = data.get(s, [])
            rpt += f'** Found {len(violations)} {SECTION_RPT[s]} **\n'
            for v in violations:
                severity = v.get('severity', 'error')
                type = v.get('type', '')
                description = v.get('description', '')
                rpt += f'[{type}]: {description}\n'
                excluded = ' (excluded)' if v.get('excluded') else ''
                rpt += f'    ; {severity}{excluded}\n'
                for item in v.get('items', []):
                    rpt += self.get_item_txt(item)
            rpt += '\n'
        rpt += '** End of Report **\n'
        return rpt

    def report(self, kind, count, data):
        if not count:
            return
        logf = logger.error if kind == 'error' else lambda msg: logger.warning(W_DRC+msg)
        logf(f'{count} DRC {kind}s detected')
        for s in JSON_SECTIONS:
            check_name = SECTION_HUMAN[s]
            for v in data.get(s, []):
                if v.get('excluded'):
                    continue
                severity = v.get('severity', 'error')
                if severity != kind:
                    continue
                type = v.get('type', '')
                description = v.get('description', '')
                txt = f'({type}) {description}\n'
                for item in v.get('items', []):
                    txt += self.get_item_txt(item)
                txt += f'    Check: {check_name}'
                logf(txt)

    def get_command(self, output):
        cmd = ['kicad-cli', 'pcb', 'drc', '-o', output, '--format', 'json', '--severity-all', '--units',
               UNITS_2_KICAD[self._units]]
        if self._schematic_parity:
            cmd.append('--schematic-parity')
        if self._all_track_errors:
            cmd.append('--all-track-errors')
        cmd.append(GS.pcb_file)
        return cmd

    @staticmethod
    def get_conf_examples(name, layers):
        if not GS.ki8:
            return None
        return [{'drc': {'format': 'HTML,CSV,JSON,RPT', 'dont_stop': True}}]
