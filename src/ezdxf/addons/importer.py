# Purpose: Import data from another DXF drawing
# Created: 27.04.13
# Copyright (c) 2013-2019, Manfred Moitzi
# License: MIT License
"""
Importer
========

This rewritten Importer class from ezdxf v0.10 is not compatible to previous ezdxf versions, but previous implementation
was already broken.

This add-on is meant to import graphical entities from another DXF drawing and their required table entries like LAYER,
LTYPE or STYLE.

Because of complex extensibility of the DXF format and the lack of sufficient documentation, I decided to remove most
of the possible source drawing dependencies from imported entities, therefore imported entities may not look
the same as the original entities in the source drawing, but at least the geometry should be the same and the DXF file
does not break.

Removed data which could contain source drawing dependencies: Extension Dictionaries, AppData and XDATA.

.. warning::

    DON'T EXPECT PERFECT RESULTS!

The new Importer() supports following data import:

  - entities which are really safe to import: LINE, POINT, CIRCLE, ARC, TEXT, SOLID, TRACE, 3DFACE, SHAPE, POLYLINE,
    ATTRIB, ATTDEF, INSERT, ELLIPSE, MTEXT, LWPOLYLINE, SPLINE, HATCH, MESH, XLINE, RAY, DIMENSION, LEADER, VIEWPORT
  - table and table entry import is restricted to LAYER, LTYPE, STYLE, DIMSTYLE
  - import of BLOCK definitions is supported
  - import of paper space layouts is supported


Import of DXF objects from the OBJECTS section is not supported.

DIMSTYLE override for entities DIMENSION and LEADER is not supported.

Example::

    import ezdxf
    from ezdxf.addons import Importer

    sdoc = ezdxf.readfile('original.dxf')
    tdoc = ezdxf.new()

    importer = Importer(sdoc, tdoc)

    # import all entities from source modelspace into modelspace of the target drawing
    importer.import_modelspace()

    # import all paperspace layouts from source drawing
    importer.import_paperspace_layouts()

    # import all CIRCLE and LINE entities from source modelspace into an arbitrary target layout.
    # create target layout
    tblock = tdoc.blocks.new('SOURCE_ENTS')
    # query source entities
    ents = sdoc.modelspace().query('CIRCLE LINE')
    # import source entities into target block
    importer.import_entities(ents, tblock)

    # This is ALWAYS the last & required step, without finalizing the target drawing is maybe invalid!
    # This step imports all additional required table entries and block definitions.
    importer.finalize()

    tdoc.saveas('imported.dxf')

"""

from typing import TYPE_CHECKING, Iterable, Set, cast, Union, List, Dict
import logging
from ezdxf.lldxf.const import DXFKeyError, DXFStructureError, DXFTableEntryError, DXFTypeError
from ezdxf.render.arrows import ARROWS

if TYPE_CHECKING:
    from ezdxf.eztypes import Drawing, DXFEntity, BaseLayout, Layout, DXFGraphic, BlockLayout, Hatch, Insert, Polyline
    from ezdxf.eztypes import DimStyle, Dimension, Viewport

logger = logging.getLogger('ezdxf')

IMPORT_TABLES = ['linetypes', 'layers', 'styles', 'dimstyles']
IMPORT_ENTITIES = {
    'LINE', 'POINT', 'CIRCLE', 'ARC', 'TEXT', 'SOLID', 'TRACE', '3DFACE', 'SHAPE', 'POLYLINE', 'ATTRIB',
    'INSERT', 'ELLIPSE', 'MTEXT', 'LWPOLYLINE', 'SPLINE', 'HATCH', 'MESH', 'XLINE', 'RAY', 'ATTDEF',
    'DIMENSION', 'LEADER',  # dimension style override not supported!
    'VIEWPORT',
}


class Importer:
    """
    The :class:`Importer` class is central element for importing data from other DXF drawings.

    Args:
        source: source :class:`~ezdxf.drawing.Drawing`
        target: target :class:`~ezdxf.drawing.Drawing`

    :ivar source: source drawing
    :ivar target: target drawing
    :ivar used_layer: Set of used layer names as string, AutoCAD accepts layer names without a LAYER table entry.
    :ivar used_linetypes: Set of used linetype names as string, these linetypes require a TABLE entry or AutoCAD will crash.
    :ivar used_styles: Set of used text style names, these text styles require a TABLE entry or AutoCAD will crash.
    :ivar used_dimstyles: Set of used dimension style names, these dimension styles require a TABLE entry or AutoCAD will crash.

    """

    def __init__(self, source: 'Drawing', target: 'Drawing'):
        self.source = source  # type: Drawing
        self.target = target  # type: Drawing

        self.used_layers = set()  # type: Set[str]
        self.used_linetypes = set()  # type: Set[str]
        self.used_styles = set()  # type: Set[str]
        self.used_dimstyles = set()  # type: Set[str]
        self.used_arrows = set()  # type: Set[str]

        # collects all imported INSERT entities, for later name resolving.
        self.imported_inserts = list()  # type: List[DXFEntity]  # imported inserts

        # collects all imported block names and their assigned new name
        # imported_block[original_name] = new_name
        self.imported_blocks = dict()  # type: Dict[str, str]
        self._default_plotstyle_handle = target.plotstyles['Normal'].dxf.handle
        self._default_material_handle = target.materials['Global'].dxf.handle

    def _add_used_resources(self, entity: 'DXFEntity') -> None:
        """ Register used resources. """
        self.used_layers.add(entity.get_dxf_attrib('layer', '0'))
        self.used_linetypes.add(entity.get_dxf_attrib('linetype', 'BYLAYER'))
        if entity.is_supported_dxf_attrib('style'):
            self.used_styles.add(entity.get_dxf_attrib('style', 'Standard'))
        if entity.is_supported_dxf_attrib('dimstyle'):
            self.used_dimstyles.add(entity.get_dxf_attrib('dimstyle', 'Standard'))

    def _add_dimstyle_resources(self, dimstyle: 'DimStyle') -> None:
        self.used_styles.add(dimstyle.get_dxf_attrib('dimtxsty', 'Standard'))
        self.used_linetypes.add(dimstyle.get_dxf_attrib('dimltype', 'BYLAYER'))
        self.used_linetypes.add(dimstyle.get_dxf_attrib('dimltex1', 'BYLAYER'))
        self.used_linetypes.add(dimstyle.get_dxf_attrib('dimltex2', 'BYLAYER'))
        self.used_arrows.add(dimstyle.get_dxf_attrib('dimblk', ''))
        self.used_arrows.add(dimstyle.get_dxf_attrib('dimblk1', ''))
        self.used_arrows.add(dimstyle.get_dxf_attrib('dimblk2', ''))
        self.used_arrows.add(dimstyle.get_dxf_attrib('dimldrblk', ''))

    def import_tables(self, table_names: Union[str, Iterable[str]] = "*", replace=False) -> None:
        """ Import DXF tables from source drawing into target drawing.

        Args:
            table_names: iterable of tables names as strings, or a single table name as string or ``*``
                         for all supported tables
            replace: True to replace already existing table entries else ignore existing entries

        Raises:
            TypeError: unsupported table type

        """
        if isinstance(table_names, str):
            if table_names == "*":  # import all supported tables
                table_names = IMPORT_TABLES
            else:  # import one specific table
                table_names = (table_names,)
        for table_name in table_names:
            self.import_table(table_name, entries="*", replace=replace)

    def import_table(self, name: str, entries: Union[str, Iterable[str]] = "*", replace=False) -> None:
        """
        Import specific table entries from source drawing into target drawing.

        Args:
            name: valid table names are ``layers``, ``linetypes`` and ``styles``
            entries: Iterable of table names as strings, or a single table name or ``*`` for all table entries
            replace: True to replace already existing table entry else ignore existing entry

        Raises:
            TypeError: unsupported table type

        """
        if name not in IMPORT_TABLES:
            raise TypeError('Table "{}" import not supported.'.format(name))
        source_table = getattr(self.source.tables, name)
        target_table = getattr(self.target.tables, name)

        if isinstance(entries, str):
            if entries == "*":  # import all table entries
                entries = (entry.dxf.name for entry in source_table)
            else:  # import just one table entry
                entries = (entries,)
        for entry_name in entries:
            try:
                table_entry = source_table.get(entry_name)
            except DXFTableEntryError:
                logger.warning('Required table entry "{}" in table {} not found.'.format(entry_name, name))
                continue
            entry_name = table_entry.dxf.name
            if entry_name in target_table:
                if replace:
                    logger.debug('Replacing already existing entry "{}" of {} table.'.format(entry_name, name))
                    target_table.remove(table_entry.dxf.name)
                else:
                    logger.debug('Discarding already existing entry "{}" of {} table.'.format(entry_name, name))
                    continue

            if name == 'layers':
                self.used_linetypes.add(table_entry.get_dxf_attrib('linetype', 'Continuous'))
            elif name == 'dimstyles':
                self._add_dimstyle_resources(table_entry)
            # duplicate table entry
            new_table_entry = self._duplicate_table_entry(table_entry)
            target_table.add_entry(new_table_entry)

    def _set_table_entry_dxf_attribs(self, entity: 'DXFEntity') -> None:
        entity.doc = self.target
        if entity.dxf.hasattr('plotstyle_handle'):
            entity.dxf.plotstyle_handle = self._default_plotstyle_handle
        if entity.dxf.hasattr('material_handle'):
            entity.dxf.material_handle = self._default_material_handle

    def _duplicate_table_entry(self, entry: 'DXFEntity') -> 'DXFEntity':
        # duplicate table entry
        new_entry = new_clean_entity(entry)
        self._set_table_entry_dxf_attribs(entry)

        # create a new handle and add entity to target entity database
        self.target.entitydb.add(new_entry)
        return new_entry

    def import_entity(self, entity: 'DXFEntity', target_layout: 'BaseLayout' = None) -> None:
        """
        Imports a single DXF `entity` into `target_layout` or the modelspace of the target drawing, if `target_layout`
        is `None`.

        Args:
            entity: DXF entity to import
            target_layout: any layout (modelspace, paperspace or block) from the target drawing

        Raises:
            DXFStructureError: `target_layout` is not a layout of target drawing

        """

        def set_dxf_attribs(e):
            e.doc = self.target
            # remove invalid resources
            e.dxf.discard('plotstyle_handle')
            e.dxf.discard('material_handle')
            e.dxf.discard('visualstyle_handle')

        if target_layout is None:
            target_layout = self.target.modelspace()
        elif target_layout.doc != self.target:
            raise DXFStructureError('Target layout has to be a layout or block from the target drawing.')

        dxftype = entity.dxftype()
        if dxftype not in IMPORT_ENTITIES:
            logger.debug('Import of {} not supported'.format(str(entity)))
            return
        self._add_used_resources(entity)

        try:
            new_entity = cast('DXFGraphic', new_clean_entity(entity))
        except DXFTypeError:
            logger.debug('Copying for DXF type {} not supported.'.format(dxftype))
            return

        set_dxf_attribs(new_entity)
        self.target.entitydb.add(new_entity)
        target_layout.add_entity(new_entity)

        try:  # additional processing
            getattr(self, '_import_' + dxftype.lower())(new_entity)
        except AttributeError:
            pass

    def _import_insert(self, insert: 'Insert'):
        self.imported_inserts.append(insert)
        # remove all possible source drawing dependencies from sub entities
        for attrib in insert.attribs:
            remove_dependencies(attrib)

    def _import_polyline(self, polyline: 'Polyline'):
        # remove all possible source drawing dependencies from sub entities
        for vertex in polyline.vertices:
            remove_dependencies(vertex)

    def _import_hatch(self, hatch: 'Hatch'):
        hatch.dxf.discard('associative')

    def _import_viewport(self, viewport: 'Viewport'):
        viewport.dxf.discard('sun_handle')
        viewport.dxf.discard('clipping_boundary_handle')
        viewport.dxf.discard('ucs_handle')
        viewport.dxf.discard('ucs_base_handle')
        viewport.dxf.discard('background_handle')
        viewport.dxf.discard('shade_plot_handle')
        viewport.dxf.discard('visual_style_handle')
        viewport.dxf.discard('ref_vp_object_1')
        viewport.dxf.discard('ref_vp_object_2')
        viewport.dxf.discard('ref_vp_object_3')
        viewport.dxf.discard('ref_vp_object_4')

    def _import_dimension(self, dimension: 'Dimension'):
        def import_arrow_blocks():
            """ Special import, because dimension blocks (arrows) must not renamed if block already exist in target
            drawing.

            """
            for insert in self.imported_inserts:
                self.import_block(insert.dxf.name, rename=False)

        block_name = dimension.get_dxf_attrib('geometry')
        if block_name:
            if block_name not in self.source.blocks:
                msg = 'Required anonymous DIMENSION block "{}" does not exist in source drawing.'.format(block_name)
                logger.error(msg)
                return

            # INSERT entities in an anonymous dimension block (arrows) gets special treatment:
            # Do NOT rename BLOCK (arrow) if already exist! -> import_arrow_blocks()
            save_imported_inserts = self.imported_inserts
            self.imported_inserts = []
            name = self.import_block(block_name, rename=True)
            dimension.dxf.geometry = name

            # special treatment for arrow blocks!
            import_arrow_blocks()
            # restore previous collected INSERT entities
            self.imported_inserts = save_imported_inserts

        else:
            logger.error('Required anonymous geometry block for DIMENSION not defined.')

    def import_entities(self, entities: Iterable['DXFEntity'], target_layout: 'BaseLayout' = None) -> None:
        """
        Import all `entities` into `target_layout` or the modelspace of the target drawing, if `target_layout` is
        `None`.

        Args:
            entities: Iterable of DXF entities
            target_layout: any layout (modelspace, paperspace or block) from the target drawing

        Raises:
            DXFStructureError: `target_layout` is not a layout of target drawing

        """
        for entity in entities:
            self.import_entity(entity, target_layout)

    def import_modelspace(self, target_layout: 'BaseLayout' = None) -> None:
        """
        Import all entities from source modelspace into `target_layout` or the modelspace of the target drawing, if
        `target_layout` is `None`.

        Args:
            target_layout: any layout (modelspace, paperspace or block) from the target drawing

        Raises:
            DXFStructureError: `target_layout` is not a layout of target drawing

        """
        self.import_entities(self.source.modelspace(), target_layout=target_layout)

    def recreate_source_layout(self, name: str) -> 'Layout':
        """
        Recreate source paperspace layout `name` in the target drawing. The layout will be renamed if `name` already
        exist in the target drawing. Returns target modelspace for layout name "Model".

        Args:
            name: layout name as string

        Raises:
            KeyError: if source layout `name` not exist

        """

        def get_target_name():
            tname = name
            base_name = name
            count = 1
            while tname in self.target.layouts:
                tname = base_name + str(count)
                count += 1

            return tname

        def clear(dxfattribs: dict) -> dict:
            def discard(name: str):
                try:
                    del dxfattribs[name]
                except KeyError:
                    pass

            discard('handle')
            discard('owner')
            discard('taborder')
            discard('shade_plot_handle')
            discard('block_record_handle')
            discard('viewport_handle')
            discard('ucs_handle')
            discard('base_ucs_handle')
            return dxfattribs

        if name.lower() == 'model':
            return self.target.modelspace()

        source_layout = self.source.layouts.get(name)  # raises KeyError
        target_name = get_target_name()
        dxfattribs = clear(source_layout.dxf_layout.dxfattribs())
        target_layout = self.target.layouts.new(target_name, dxfattribs=dxfattribs)
        return target_layout

    def import_paperspace_layout(self, name: str) -> 'Layout':
        """
        Import paperspace layout `name` into target drawing. Recreates the source paperspace layout in the target
        drawing, renames the target paperspace if already a paperspace with same `name` exist and imports all
        entities from source paperspace into target paperspace.

        Args:
            name: source paper space name as string

        Returns: new created target paperspace :class:`Layout`

        Raises:
            KeyError: source paperspace does not exist
            DXFTypeError: invalid modelspace import

        """
        if name.lower() == 'model':
            raise DXFTypeError('Can not import modelspace, use method import_modelspace().')
        source_layout = self.source.layouts.get(name)
        target_layout = self.recreate_source_layout(name)
        self.import_entities(source_layout, target_layout)
        return target_layout

    def import_paperspace_layouts(self) -> None:
        """
        Import all paperspace layouts and their content into target drawing. Target layouts will be renamed if already
        a layout with same name exist. Layouts will be imported in original tab order.

        """
        for name in self.source.layouts.names_in_taborder():
            if name.lower() != 'model':  # do not import modelspace
                self.import_paperspace_layout(name)

    def import_blocks(self, block_names: Iterable[str], rename=False) -> None:
        """
        Import all block definitions. If block already exist the block will be renamed if argument `rename` is True,
        else the existing target block will be used instead of the source block. Required name resolving for imported
        block references (INSERT), will be done in :meth:`Importer.finalize`.

        Args:
            block_names: names of blocks to import
            rename: rename block if exists in target drawing

        Raises:
            ValueError: source block not found

        """
        for block_name in block_names:
            self.import_block(block_name, rename=rename)

    def import_block(self, block_name: str, rename=True) -> str:
        """
        Import one block definition. If block already exist the block will be renamed if argument `rename` is True,
        else the existing target block will be used instead of the source block. Required name resolving for imported
        block references (INSERT), will be done in :meth:`Importer.finalize`.

        To replace an existing block in the target drawing, just delete it before importing:
        :code:`target.blocks.delete_block(block_name, safe=False)`

        Args:
            block_name: name of block to import
            rename: rename block if exists in target drawing

        Returns: block name (renamed)

        Raises:
            ValueError: source block not found

        """

        def get_new_block_name() -> str:
            num = 0
            name = block_name
            while name in target_blocks:
                name = block_name + str(num)
                num += 1
            return name

        try:  # already imported block?
            return self.imported_blocks[block_name]
        except KeyError:
            pass

        try:
            source_block = self.source.blocks[block_name]
        except DXFKeyError:
            raise ValueError('Source block "{}" not found.'.format(block_name))

        target_blocks = self.target.blocks
        if (block_name in target_blocks) and (rename is False):
            self.imported_blocks[block_name] = block_name
            return block_name

        new_block_name = get_new_block_name()
        block = source_block.block
        target_block = target_blocks.new(new_block_name, base_point=block.dxf.base_point, dxfattribs={
            'description': block.dxf.description,
            'flags': block.dxf.flags,
            'xref_path': block.dxf.xref_path,
        })
        self.import_entities(source_block, target_layout=target_block)
        self.imported_blocks[block_name] = new_block_name
        return new_block_name

    def _create_missing_arrows(self):
        """
        Create or import required arrows, used by LEADER or DIMSTYLE, which are not imported automatically because they
        are not actually used in an anonymous  DIMENSION blocks.

        """
        self.used_arrows.discard('')  # standard default arrow '' needs no block definition
        for arrow_name in self.used_arrows:
            if ARROWS.is_acad_arrow(arrow_name):
                self.target.acquire_arrow(arrow_name)
            else:
                self.import_block(arrow_name, rename=False)

    def _resolve_inserts(self) -> None:
        """
        Resolve block names of imported block reference entities (INSERT).

        This is required for the case the name of the imported block collides with an already existing block
        in the target drawing and conflict resolving method was ``rename``.

        """
        while len(self.imported_inserts):
            inserts = list(self.imported_inserts)
            # clear imported inserts, block import may append additional inserts
            self.imported_inserts = []
            for insert in inserts:
                block_name = self.import_block(insert.dxf.name)
                insert.dxf.name = block_name

    def _import_required_table_entries(self) -> None:
        """
        Import required tables entries collected while importing entities into target drawing.

        """
        # 1. dimstyles import adds additional required linetype and style resources and required arrows
        if len(self.used_dimstyles):
            self.import_table('dimstyles', self.used_dimstyles)

        # 2. layers import adds additional required linetype resources
        if len(self.used_layers):
            self.import_table('layers', self.used_layers)

        # linetypes and styles do not add additional required resources
        if len(self.used_linetypes):
            self.import_table('linetypes', self.used_linetypes)
        if len(self.used_styles):
            self.import_table('styles', self.used_styles)

    def finalize(self) -> None:
        """
        Finalize import by importing required table entries and block definition, without finalization the target
        drawing is maybe invalid fore AutoCAD. Call :meth:`~Importer.finalize()` as last step of the import process.

        """
        self._resolve_inserts()
        self._import_required_table_entries()
        self._create_missing_arrows()


def new_clean_entity(entity: 'DXFEntity', xdata: bool = False) -> 'DXFEntity':
    """
    Copy entity and remove all external dependencies.

    Args:
        entity: DXF entity
        xdata: remove xdata flag

    """
    new_entity = entity.copy()
    # clear drawing link
    new_entity.doc = None
    return remove_dependencies(new_entity, xdata=xdata)


def remove_dependencies(entity: 'DXFEntity', xdata: bool = False) -> 'DXFEntity':
    """
    Remove all external dependencies.

    Args:
        entity: DXF entity
        xdata: remove xdata flag

    """
    entity.appdata = None
    entity.reactors = None
    entity.extension_dict = None
    if not xdata:
        entity.xdata = None
    return entity