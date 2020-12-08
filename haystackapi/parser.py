#!/usr/bin/python
# -*- coding: utf-8 -*-
# Zinc Grid Metadata
# (C) 2016 VRT Systems
#
# vim: set ts=4 sts=4 et tw=78 sw=4 si:
import functools
import json
import logging
import re

from .csvparser import parse_grid as parse_csv_grid
from .csvparser import parse_scalar as parse_csv_scalar
from .jsonparser import parse_grid as parse_json_grid, \
    parse_scalar as parse_json_scalar
# Bring in version handling
from .version import Version, LATEST_VER
from .zincparser import parse_grid as parse_zinc_grid, \
    parse_scalar as parse_zinc_scalar

LOG = logging.getLogger(__name__)

# Trailing newline sanitation
TRAILING_NL_RE = re.compile(r'\n+$')

GRID_SEP = re.compile(r'(?<=\n)\n+')  # It's may be not enough if a string has an empty line

MODE_ZINC = 'text/zinc'
MODE_JSON = 'application/json'
MODE_CSV = 'text/csv'

_suffix_to_mode = {".zinc": MODE_ZINC,
                   ".json": MODE_JSON,
                   ".csv": MODE_CSV
                   }

_mode_to_suffix = {MODE_ZINC: ".zinc",
                   MODE_JSON: ".json",
                   MODE_CSV: ".csv"
                   }


def suffix_to_mode(ext):
    """ Convert a file suffix to Haystack mode"""
    return _suffix_to_mode.get(ext, None)


def mode_to_suffix(mode):
    """ Convert haystackapi mode to file suffix"""
    return _mode_to_suffix.get(mode, None)


def _parse_mode(mode):
    """
    Sanitise the mode given.  Whilst code _should_ use the MODE_ZINC and
    MODE_JSON constants above, we have some code internally that plays fast
    and loose with this.
    """
    if mode in (MODE_ZINC, MODE_JSON, MODE_CSV):
        return mode

    # Danger zone: user has given us something different.  They better know
    # what it is they are doing!
    mode = str(mode).lower()
    if mode == 'zinc':
        LOG.warning("Use MODE_ZINC in place of 'zinc'")
        return MODE_ZINC
    if mode == 'json':
        LOG.warning("Use MODE_JSON in place of 'json'")
        return MODE_JSON
    if mode == 'csv':
        LOG.warning("Use MODE_CSV in place of 'csv'")
        return MODE_JSON
    # Clearly that was a wrong assumption.  Let 'em have it!
    raise ValueError('Unrecognised mode, should be MODE_ZINC or MODE_JSON')


def parse(grid_str, mode=MODE_ZINC, charset='utf-8', single=True):
    """
    Parse the given Zinc text and return the equivalent data.
    """
    # Sanitise mode
    mode = _parse_mode(mode)

    # Decode incoming text (or python3 will whine!)
    if isinstance(grid_str, bytes):
        grid_str = grid_str.decode(encoding=charset)

    # Split the separate grids up, the grammar definition has trouble splitting
    # them up normally.  This will truncate the newline off the end of the last
    # row.
    _parse = functools.partial(parse_grid, mode=mode,
                               charset=charset)
    if mode == MODE_JSON:
        if isinstance(grid_str, str):
            grid_data = json.loads(grid_str)
        else:
            grid_data = grid_str

        # Normally JSON only permits a single grid, but we'll support an
        # extension where a JSON array of grid objects represents multiple.
        # To simplify programming, we'll "normalise" to array-of-grids here.
        if isinstance(grid_data, dict):
            grid_data = [grid_data]
    else:
        if not single:
            grid_data = GRID_SEP.split(TRAILING_NL_RE.sub('\n', grid_str))
        else:
            grid_data = [grid_str]

    grids = list(map(_parse, grid_data))
    if single:
        # Most of the time, we will only want one grid.
        if grids:
            return grids[0]
        return None
    return grids


def parse_grid(grid_str, mode=MODE_ZINC, charset='utf-8'):
    # Sanitise mode
    mode = _parse_mode(mode)

    # Decode incoming text
    if isinstance(grid_str, bytes):  # pragma: no cover
        # No coverage here, because it *should* be handled above unless the user
        # is preempting us by calling `parse_grid` directly.
        grid_str = grid_str.decode(encoding=charset)

    if mode == MODE_ZINC:
        return parse_zinc_grid(grid_str)
    if mode == MODE_JSON:
        return parse_json_grid(grid_str)
    if mode == MODE_CSV:
        return parse_csv_grid(grid_str)
    raise NotImplementedError('Format not implemented: %s' % mode)


def parse_scalar(scalar, mode=MODE_ZINC, version=LATEST_VER, charset='utf-8'):
    # Sanitise mode
    mode = _parse_mode(mode)

    # Decode version string
    if not isinstance(version, Version):
        version = Version(version)

    # Decode incoming text
    if isinstance(scalar, bytes):
        scalar = scalar.decode(encoding=charset)

    if mode == MODE_ZINC:
        return parse_zinc_scalar(scalar, version=version)
    if mode == MODE_JSON:
        return parse_json_scalar(scalar, version=version)
    if mode == MODE_CSV:
        return parse_csv_scalar(scalar, version=version)
    raise NotImplementedError('Format not implemented: %s' % mode)