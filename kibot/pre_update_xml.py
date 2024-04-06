# -*- coding: utf-8 -*-
# Copyright (c) 2020-2023 Salvador E. Tropea
# Copyright (c) 2020-2023 Instituto Nacional de Tecnología Industrial
# License: GPL-3.0
# Project: KiBot (formerly KiPlot)
"""
Dependencies:
  - from: KiAuto
    role: mandatory
    command: eeschema_do
    version: 1.5.4
"""
from collections import namedtuple
import os
import xml.etree.ElementTree as ET
from .macros import macros, document, pre_class  # noqa: F401
from .error import KiPlotConfigurationError
from .gs import GS
from .kiplot import load_board
from .misc import BOM_ERROR, NETLIST_DIFF, W_PARITY, MISSING_TOOL, KICAD_VERSION_7_0_1, W_NOTINBOM, MOD_BOARD_ONLY, W_DEPR
from .log import get_logger
from .optionable import Optionable
import pcbnew

logger = get_logger(__name__)
Component = namedtuple("Component", "val fp props")


class Update_XMLOptions(Optionable):
    """ Reference sorting options """
    def __init__(self):
        super().__init__()
        with document:
            self.enabled = True
            """ Enable the update. This is the replacement for the boolean value """
            self.check_pcb_parity = False
            """ *Check if the PCB and Schematic are synchronized.
                This is equivalent to the *Test for parity between PCB and schematic* of the DRC dialog.
                Not available for KiCad 5. **Important**: when using KiCad 6 and the *Exclude from BoM* attribute
                these components won't be included in the generated XML, so we can't check its parity """
            self.as_warnings = False
            """ Inform the problems as warnings and don't stop """


@pre_class
class Update_XML(BasePreFlight):  # noqa: F821
    """ [boolean=false|dict] Update the XML version of the BoM (Bill of Materials).
        To ensure our generated BoM is up to date.
        Note that this isn't needed when using the internal BoM generator (`bom`).
        You can compare the PCB and schematic netlists using it """
    def __init__(self, name, value):
        super().__init__(name, value)
        self._check_pcb_parity = False
        self._sch_related = True

    def config(self):
        if isinstance(self._value, bool):
            self._enabled = self._value
        elif isinstance(self._value, dict):
            f = Update_XMLOptions()
            f.set_tree(self._value)
            f.config(self)
            self._enabled = f.enabled
            self._check_pcb_parity = f.check_pcb_parity
            self.options = f
            self._pcb_related = True
        else:
            raise KiPlotConfigurationError('must be boolean or dict')

    @classmethod
    def get_doc(cls):
        return cls.__doc__, Update_XMLOptions

    def get_targets(self):
        """ Returns a list of targets generated by this preflight """
        return [GS.sch_no_ext+'.xml']

    def check_components(self, comps, errors):
        found_comps = set()
        excluded = set()
        for m in GS.get_modules():
            ref = m.GetReference()
            pcb_props = GS.get_fields(m)
            found_comps.add(ref)
            if ref not in comps:
                if GS.ki6_only and pcb_props.get('exclude_from_bom') is not None:
                    # KiCad 6 doesn't include the excluded components in the netlist
                    logger.warning(W_NOTINBOM+f"{ref} excluded from BoM we can't check its parity, upgrade to KiCad 7")
                    excluded.add(ref)
                else:
                    if not m.GetAttributes() & MOD_BOARD_ONLY:
                        errors.append('{} found in PCB, but not in schematic'.format(ref))
                continue
            sch_data = comps[ref]
            pcb_fp = m.GetFPIDAsString()
            if sch_data.fp != pcb_fp:
                errors.append('{} footprint mismatch (PCB: `{}` vs schematic: `{}`)'.format(ref, pcb_fp, sch_data.fp))
            pcb_val = m.GetValue()
            if sch_data.val != pcb_val:
                errors.append('{} value mismatch (PCB: `{}` vs schematic: `{}`)'.format(ref, pcb_val, sch_data.val))
            # Properties
            found_props = set()
            for p, v in sch_data.props.items():
                v_pcb = pcb_props.get(p)
                if v_pcb is None:
                    errors.append('{} schematic property `{}` not in PCB'.format(ref, p))
                    continue
                found_props.add(p)
                if v_pcb != v:
                    if v is None:
                        # Things like "exclude_from_bom" has no "value", so we get None, but they have '' in the PCB
                        v = ''
                    elif p == 'Sheetfile':
                        # Sheetfile is really inside the .kicad_pcb, but is just generated by Eeschema
                        # This implies that Eeschema can add a path relative to cwd
                        # We just check the filename here
                        v_pcb = os.path.basename(v_pcb)
                        v = os.path.basename(v)
                    if v_pcb != v:
                        errors.append(f'{ref} property `{p}` mismatch (PCB: `{v_pcb}` vs schematic: `{v}`)')
            # Missing properties
            for p in set(pcb_props.keys()).difference(found_props):
                errors.append('{} PCB property `{}` not in schematic'.format(ref, p))
        for ref in set(comps.keys()).difference(found_comps):
            if 'exclude_from_board' in comps[ref].props:
                continue
            errors.append('{} found in schematic, but not in PCB'.format(ref))
        return excluded

    def check_nets(self, net_nodes, errors, excluded):
        # Total count
        con = GS.board.GetConnectivity()
        pcb_net_count = con.GetNetCount()-1  # Removing the bogus net 0
        sch_net_count = len(net_nodes)
        if pcb_net_count != sch_net_count:
            errors.append('Net count mismatch (PCB {} vs schematic {})'.format(pcb_net_count, sch_net_count))
        net_info = GS.board.GetNetInfo()
        # Names and connection
        pcb_net_names = set()
        for n in net_info.NetsByNetcode():
            if not n:
                # Bogus net code 0
                continue
            net = net_info.GetNetItem(n)
            net_name = net.GetNetname()
            if net_name not in net_nodes:
                errors.append('Net `{}` not in schematic'.format(net_name))
                continue
            pcb_net_names.add(net_name)
            sch_nodes = net_nodes[net_name]
            pcb_nodes = {pad.GetParent().GetReference()+' pin '+pad.GetNumber()
                         for pad in con.GetNetItems(n, pcbnew.PCB_PAD_T)
                         if pad.GetParent().GetReference() not in excluded}
            dif = pcb_nodes-sch_nodes
            if dif:
                errors.append('Net `{}` extra PCB connection/s: {}'.format(net_name, ','.join(list(dif))))
            dif = sch_nodes-pcb_nodes
            if dif:
                errors.append('Net `{}` missing PCB connection/s: {}'.format(net_name, ','.join(list(dif))))
        # Now check if the schematic added nets
        for name in net_nodes.keys():
            if name not in pcb_net_names:
                errors.append('Net `{}` not in PCB'.format(name))

    def check_pcb_parity(self):
        if GS.ki5:
            GS.exit_with_error('PCB vs schematic parity only available for KiCad 6', MISSING_TOOL)
        if GS.ki7 and GS.kicad_version_n < KICAD_VERSION_7_0_1:
            GS.exit_with_error("Connectivity API is broken on KiCad 7.0.0\n"
                               "Please upgrade KiCad to 7.0.1 or newer", MISSING_TOOL)
        if GS.ki8:
            logger.warning(W_DEPR+'For KiCad 8 use the `drc` preflight, it supports parity checks from KiCad')
        fname = GS.sch_no_ext+'.xml'
        logger.debug('Loading XML: '+fname)
        try:
            tree = ET.parse(fname)
        except Exception as e:
            raise KiPlotConfigurationError('Errors parsing {}\n{}'.format(fname, e))
        root = tree.getroot()
        if root.tag != 'export':
            raise KiPlotConfigurationError("{} isn't a valid netlist".format(fname))
        # Check version? root.attrib.get('version')
        components = root.find('components')
        comps = {}
        if components is not None:
            for c in components.iter('comp'):
                ref = c.attrib.get('ref')
                val = c.find('value')
                val = val.text if val is not None else ''
                fp = c.find('footprint')
                fp = fp.text if fp is not None else ''
                props = {p.get('name'): p.get('value') for p in c.iter('property')}
                logger.debugl(2, '- {}: {} {} {}'.format(ref, val, fp, props))
                comps[ref] = Component(val, fp, props)
        netlist = root.find('nets')
        net_nodes = {}
        if netlist is not None:
            for n in netlist.iter('net'):
                # This is a useless number stored there just to use disk space and confuse people:
                # code = int(n.get('code'))
                net_nodes[n.get('name')] = {node.get('ref')+' pin '+node.get('pin') for node in n.iter('node')}
        # Check with the PCB
        errors = []
        load_board()
        # Check components
        excluded = self.check_components(comps, errors)
        # Check the nets
        self.check_nets(net_nodes, errors, excluded)
        # Report errors
        if errors:
            if self.options.as_warnings:
                for e in errors:
                    logger.warning(W_PARITY+e)
            else:
                GS.exit_with_error(errors, NETLIST_DIFF)

    def run(self):
        command = self.ensure_tool('KiAuto')
        out_dir = self.expand_dirname(GS.out_dir)
        cmd = [command, 'bom_xml', GS.sch_file, out_dir]
        # If we are in verbose mode enable debug in the child
        cmd = self.add_extra_options(cmd)
        # While creating the XML we run a BoM plug-in that creates a useless BoM
        # We remove it, unless this is already there
        side_effect_file = os.path.join(out_dir, GS.sch_basename+'.csv')
        if not os.path.isfile(side_effect_file):
            self._files_to_remove.append(side_effect_file)
        logger.info('- Updating BoM in XML format')
        self.exec_with_retry(cmd, BOM_ERROR)
        if self._check_pcb_parity:
            self.check_pcb_parity()
