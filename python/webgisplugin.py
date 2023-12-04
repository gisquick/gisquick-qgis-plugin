# -*- coding: utf-8 -*-
"""
/***************************************************************************
 Gisquick plugin
 Publish your projects into Gisquick application
 ***************************************************************************/
"""
import os
import re
import sys
import math
import urllib
import hashlib
import platform
import configparser
from urllib.parse import parse_qs, urlparse, urljoin, unquote

# Import the PyQt and QGIS libraries
import PyQt5.uic
from PyQt5.QtCore import QDir, QFileInfo
from qgis.core import (
    Qgis, QgsMapLayer, QgsProject, QgsLayerTreeLayer, QgsLayerTreeGroup, QgsLayoutItemLabel, QgsCoordinateReferenceSystem,
    QgsMapLayerType, QgsWkbTypes, QgsUnitTypes, QgsDataSourceUri, QgsFieldConstraints, QgsCoordinateTransform
)
# from qgis.server import QgsServerProjectUtils
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QPushButton
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QSettings, QTranslator, qVersion, QCoreApplication

# Initialize Qt resources from file resources.py
from . import resources_rc

from .utils import scales_to_resolutions, resolutions_to_scales, to_decimal_array
from .gisquick_ws import gisquick_ws, WsError


__metadata__ = configparser.ConfigParser()
__metadata__.read(os.path.join(os.path.dirname(__file__), 'metadata.txt'))


# from qgis.PyQt import QtCore
from PyQt5 import QtCore
from PyQt5.QtCore import QThread, QTimer


Types = {
    'bool': [1],
    'int': [2, 4, 32, 33],
    'uint': [3, 5, 35, 36],
    'float': [6, 38],
    'text': [7, 10, 34],
    'date': [14],
    'time': [15],
    'datetime': [16]
}
FieldTypes = {}
for t, codes in Types.items():
    FieldTypes.update({c: t for c in codes})


def clean_data(data):
    return {k:v for k, v in data.items() if v or v == False}


UnitsExtentPrecision = {
  'mm': 0,
  'cm': 0,
  'feet': 0,
  'yd': 0,
  'meters': 0,
  'km': 2,
  'mi': 2,
  'nautical miles': 2,
  'degrees': 6
}

def format_extent(extent, crs):
    if extent:
        for n in extent:
            if math.isinf(n):
                return None
        unit = QgsUnitTypes.encodeUnit(crs.mapUnits())
        precision = UnitsExtentPrecision.get(unit, 3)
        return [round(v, precision) for v in extent]


def flags_list(**params):
    return [k for k, v in params.items() if v]


class WebGisPlugin(object):

    dialog = None
    project = None
    ws = None

    def __init__(self, iface):
        # Save reference to the QGIS interface
        self.iface = iface
        # initialize plugin directory
        self.plugin_dir = os.path.dirname(__file__)
        # initialize locale
        locale = QSettings().value("locale/userLocale")[0:2]
        localePath = os.path.join(self.plugin_dir, 'i18n', 'gisquick_{}.qm'.format(locale))

        if os.path.exists(localePath):
            self.translator = QTranslator()
            self.translator.load(localePath)

            if qVersion() > '4.3.3':
                QCoreApplication.installTranslator(self.translator)

    def initGui(self):
        # Create action that will start plugin configuration
        self.action = QAction(
            QIcon(":/plugins/gisquick-dev/img/icon.svg"),
            u"Publish in Gisquick", self.iface.mainWindow())
        self.action.setCheckable(True)
        # connect the action to the run method
        self.action.triggered.connect(self.toggle_tool)

        self.settings_action = QAction(
            QIcon(":/plugins/gisquick-dev/img/settings.svg"),
            u"Configure", self.iface.mainWindow())
        # connect the action to the run method
        self.settings_action.triggered.connect(self.show_settings)

        # Add toolbar button and menu item
        # self.iface.addToolBarIcon(self.action)
        self.iface.addWebToolBarIcon(self.action)
        self.iface.addPluginToWebMenu(u"&Gisquick", self.action)
        self.iface.addPluginToWebMenu(u"&Gisquick", self.settings_action)

    def unload(self):
        if self.ws:
            gisquick_ws.stop()
            self.ws = None

        # Remove the plugin menu item and icon
        self.iface.removePluginWebMenu(u"&Gisquick", self.action)
        self.iface.removePluginWebMenu(u"&Gisquick", self.settings_action)
        self.iface.removeWebToolBarIcon(self.action)


    def get_layer_attributes(self, layer):
        excluded_attributes = layer.excludeAttributesWfs()
        # layer.fields().configurationFlags() # new API since 3.16, but not available in python
        # https://api.qgis.org/api/classQgsFields.html

        attributes = []
        for f in layer.fields():
            if f.name() in excluded_attributes:
                continue
            constraints = int(f.constraints().constraints())
            widget = f.editorWidgetSetup()
            data_type = FieldTypes.get(f.type())
            constrains = {
                "not_null": constraints & QgsFieldConstraints.ConstraintNotNull,
                "unique": constraints & QgsFieldConstraints.ConstraintUnique,
                "readonly": f.isReadOnly() if hasattr(f, "isReadOnly") else False,
            }
            data = {
                "name": f.name(),
                "alias": f.alias(),
                "type": data_type,
                "note": f.comment(),
                "constrains": [k for k, v in constrains.items() if v],
                # "allow_null": not(constraints & QgsFieldConstraints.ConstraintNotNull),
                "widget": widget.type(),
                "config": widget.config()
            }
            attributes.append(clean_data(data))
        return attributes


    def get_layers_tree(self):
        def visit_node(tree_node):
            if isinstance(tree_node, QgsLayerTreeLayer):
                if tree_node.layerId():
                    return {
                      "id": tree_node.layerId()
                    }
            elif isinstance(tree_node, QgsLayerTreeGroup):
                children = []
                
                # print(tree_node.name(), tree_node.customProperty("wmsShortName"))
                for child_tree_node in tree_node.children():
                    info = visit_node(child_tree_node)
                    if info:
                        children.append(info)
                group = {
                    "name": tree_node.name(),
                    "layers": children
                }
                if tree_node.isMutuallyExclusive():
                    group["mutually_exclusive"] = True
                return group

        root_node = self.iface.layerTreeView().layerTreeModel().rootGroup()
        return visit_node(root_node)["layers"]


    def get_background_layers(self):
        project = QgsProject.instance()
        layers = project.mapLayers().items()
        return [lid for lid, layer in layers if layer.customProperty("WMSBackgroundLayer")]


    def get_bookmarks(self):
        project = QgsProject.instance()
        bookmarkManager = project.bookmarkManager()
        result = {}
        
        for bookmark in bookmarkManager.bookmarks():
            group_name = bookmark.group() or 'default'
            if not result.get(group_name):
                result[group_name] = {}

            extent = bookmark.extent()
            if not extent.isEmpty():
                transform = QgsCoordinateTransform(bookmark.extent().crs(), project.crs(), QgsProject.instance())
                extent = transform.transform(extent)
                extent = extent.toRectF().getCoords()
            else:
                extent = None

            id = bookmark.id()

            result[group_name][id] = {
                "id": id,
                "name": bookmark.name(),
                "rotation": getattr(bookmark, 'rotation', float)(),
                "extent": format_extent(extent, project.crs()),
                "group": group_name,
            }

        return result


    def get_project_layers(self, skip_layers_with_error=False):
        dbname_pattern = re.compile("dbname='([^']+)'")
        project = QgsProject.instance()
        # non_identifiable_layers = project.readListEntry("Identify", "/disabledLayers")[0] or []
        # wfs_layers = project.readListEntry("WFSLayers", "/")[0] or []
        # wfs_layers = QgsServerProjectUtils.wfsLayerIds(project)

        map_canvas = self.iface.mapCanvas()
        map_settings = map_canvas.mapSettings()
        rm = project.relationManager()
        
        # wfs_info = {
        #     "query": QgsServerProjectUtils.wfsLayerIds(project),
        #     "insert": QgsServerProjectUtils.wfstInsertLayerIds(project),
        #     "update": QgsServerProjectUtils.wfstUpdateLayerIds(project),
        #     "delete": QgsServerProjectUtils.wfstDeleteLayerIds(project)
        # }
        wfs_info = {
            "query": project.readListEntry("WFSLayers", "/")[0],
            "insert": project.readListEntry("WFSTLayers", "Insert")[0],
            "update": project.readListEntry("WFSTLayers", "Update")[0],
            "delete": project.readListEntry("WFSTLayers", "Delete")[0]
        }
        projDir = QDir(project.absolutePath())
        def relativePath(path):
            return projDir.relativeFilePath(path)

        # if project.layerTreeRoot().hasCustomLayerOrder():
        #     layers_order = project.layerTreeRoot().customLayerOrder()
        # else:
        #     layers_order = [tree_layer.layer() for tree_layer in QgsProject.instance().layerTreeRoot().findLayers()]

        data = {}
        for lid, layer in project.mapLayers().items():
            if not project.layerTreeRoot().findLayer(lid):
                continue
            try:
                source = layer.source()
                dp = layer.dataProvider()
                provider_type = layer.providerType()
                # qgis 3.16 returns empty provider_type for VectorTileLayer
                if not provider_type and layer.type() == QgsMapLayerType.VectorTileLayer:
                    provider_type = "vectortile"

                identifiable = bool(QgsMapLayer.LayerFlag(layer.flags()) & QgsMapLayer.Identifiable)
                flags = []
                # uri = ""

                # todo: handle 'delimitedtext', 'gpx'
                source_params = None
                if not provider_type or provider_type in ("wms", "vectortile"):
                    source_params = parse_qs(source)
                    source_params = {k: v if len(v) > 1 else v[0] for k, v in source_params.items()}
                    # uri = source_params["url"][0]
                elif provider_type == "postgres":
                    uri = dp.uri()
                    source_params = {
                        "host": uri.host(),
                        "port": uri.port(),
                        "driver": uri.driver(),
                        "dbname": uri.database(),
                        "schema": uri.schema(),
                        "table": uri.table(),
                        "sql": uri.sql()
                        # "username": uri.username()
                    }
                    # uri = "postgresql://%s:%s" % (dp.uri().host(), dp.uri().port())
                elif provider_type == "spatialite":
                    # match = dbname_pattern.search(source)
                    # if match:
                    #     uri = "file://%s" % match.group(1)
                    uri = dp.uri()
                    source_params = {
                        "file": relativePath(uri.database()),
                        "schema": uri.schema(),
                        "table": uri.table(),
                        "sql": uri.sql()
                    }
                elif provider_type in ("WFS", "arcgismapserver"):
                    uri = dp.uri()
                    # would be better to use uri.parameterKeys() in the future (since QGIS 3.26)
                    # if hasattr(uri, "parameterKeys"):
                    #     params = uri.parameterKeys()
                    if provider_type == "WFS":
                        params = ["url", "typename", "srsname", "version", "pagingEnabled", "restrictToRequestBBOX", "maxNumFeatures"]
                    else:
                        params = ["url", "layer", "crs", "format"]
                    source_params = { p: uri.param(p) for p in params if uri.hasParam(p) }
                elif provider_type in ("ogr", "gdal"):
                    # uri = "file://%s" % source.split("|")[0]
                    parts = source.split("|")
                    params = dict([p.split("=", 1) for p in parts[1:]])
                    source_params = { **params, "file": relativePath(parts[0]) }
                else:
                    uri = source
                    try:
                        u = urlparse(source)
                        # print(u.scheme)
                        # print(u)
                        if u.scheme == "file":
                            source_params = {
                                "file": relativePath(unquote(u.path))
                            }
                    except:
                        pass

                extent = layer.extent()
                if not extent.isEmpty():
                    extent = map_settings.layerExtentToOutputExtent(
                        layer,
                        layer.extent()
                    ).toRectF().getCoords()
                else:
                    extent = None
                meta = {
                    "id": layer.id(),
                    "title": layer.title() or layer.name(),
                    "name": layer.shortName() or layer.name(),
                    # "server_name": layer.shortName() if hasattr(layer, "shortName") else layer.name(),
                    "provider_type": provider_type,
                    "projection": layer.crs().authid(),
                    # "type": layer_type,
                    "type": layer.type().name,
                    # "source": source,
                    "source_params": source_params,
                    # "source2": QgsDataSourceUri.removePassword(source),
                    # "source": uri,
                    "extent": format_extent(extent, project.crs()),
                    "visible": project.layerTreeRoot().findLayer(lid).isVisible(), # or check if in mapCanvas.layers()
                    # "identifiable": identifiable,
                    "metadata": clean_data({
                        # "title": layer.title(),
                        "abstract": layer.abstract(),
                        "keyword_list": layer.keywordList(),
                        "data_url": layer.dataUrl(),
                        "data_url_format": layer.dataUrlFormat()
                    })
                }

                # if layer in layers_order:
                #     meta["drawing_order"] = layers_order.index(layer)
                legend_url = layer.legendUrl()
                if legend_url:
                    meta["legend_url"] = legend_url
                if layer.attribution():
                    meta["attribution"] = {
                        "title": layer.attribution(),
                        "url": layer.attributionUrl()
                    }

                opts = None
                if layer.type() == QgsMapLayerType.VectorLayer:
                    attributes = self.get_layer_attributes(layer)
                    if attributes:
                        meta["attributes"] = attributes
                    wfs = {
                        "query": layer.id() in wfs_info["query"],
                        "insert": layer.id() in wfs_info["insert"],
                        "update": layer.id() in wfs_info["update"],
                        "delete": layer.id() in wfs_info["delete"]
                    }
                    wfs_flags = flags_list(**wfs)
                    opts = {
                        "wkb_type": QgsWkbTypes.displayString(layer.wkbType()),
                        "labels": layer.labelsEnabled(),
                        "wfs": wfs_flags
                    }
                    queryable = identifiable and wfs["query"]
                    flags.extend(flags_list(
                        query=queryable,
                        edit=not(layer.readOnly()) and len(wfs_flags) > 1 # (query && (insert || update || delete))
                    ))

                    relations = [r for r in rm.referencedRelations(layer) if r.isValid()]
                    if relations:
                        relations_data = []
                        for rel in relations:
                            rl = rel.referencingLayer()
                            rl_fields = rel.referencingLayer().fields()
                            referencing_fields = [rl_fields.at(fi).name() for fi in rel.referencingFields()]
                            referenced_fields = [layer.fields().at(fi).name() for fi in rel.referencedFields()]
                            relations_data.append({
                                "name": rel.name(),
                                "referencing_layer": rl.shortName() or rl.name(),
                                "strength": rel.strength(),
                                "referencing_fields": referencing_fields,
                                "referenced_fields": referenced_fields,
                            })
                        meta["relations"] = relations_data

                elif layer.type() == QgsMapLayerType.RasterLayer:
                    # meta["queryable"] = identifiable
                    flags.extend(flags_list(
                        query=identifiable
                    ))
                    bands = [layer.bandName(i) for i in range(1, layer.bandCount() + 1)]
                    if bands:
                        meta["bands"] = bands
                    opts = {
                        "native_resolutions": dp.nativeResolutions()
                    }

                if opts:
                    meta["options"] = opts
                meta["flags"] = flags
                data[lid] = meta

            except Exception as e:
                if not skip_layers_with_error:
                    raise Exception("Failed to collect metadata of layer '%s'" % layer.name()) from e

                    # tb = sys.exc_info()[2]
                    # raise MetadataException("Failed to collect metadata of layer '%s'" % layer.name()).with_traceback(tb) from None
        return data


    def get_print_templates(self):
        composer_templates = []
        layout_manager = QgsProject.instance().layoutManager()
        for layout in layout_manager.printLayouts():
            map = layout.referenceMap()
            units_conversion = map.mapUnitsToLayoutUnits()
            composer_data = {
                'name': layout.name(),
                'width': layout.layoutBounds().width(),
                'height': layout.layoutBounds().height(),
                'map': {
                    'name': 'map0',
                    'x': map.pagePos().x(),
                    'y': map.pagePos().y(),
                    'width': map.extent().width() * units_conversion,
                    'height': map.extent().height() * units_conversion
                },
                'labels': [
                    item.id() for item in list(layout.items())
                        if isinstance(item, QgsLayoutItemLabel) and item.id()
                ]
            }
            grid = map.grid()
            if grid.enabled():
                composer_data['map']['grid'] = {
                    'intervalX': grid.intervalX(),
                    'intervalY': grid.intervalY(),
                }
            composer_templates.append(composer_data)
        return composer_templates

    def get_project_info(self, skip_layers_with_error=False):
        project = QgsProject.instance()
        project_crs = project.crs()
        map_canvas = self.iface.mapCanvas()

        # scales, _ = project.readListEntry("Scales", "/ScalesList")
        # scales = [int(s.split(":")[1]) for s in scales]

        view_settings = project.viewSettings()
        scales = view_settings.mapScales()

        projections = {}
        crs_list = [project_crs] + [l.crs() for l in project.mapLayers().values()]
        tc = project.transformContext()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

        for crs in crs_list:
            if crs.isValid() and crs.authid() and crs.authid() not in projections:
                projections[crs.authid()] = {
                    "is_geographic": crs.isGeographic(),
                    # calculateCoordinateOperation seems to not always produce valid proj4 definitions
                    # "proj4": tc.calculateCoordinateOperation(wgs84, crs) or crs.toProj4()
                    "proj4": crs.toProj4()
                }

        units = {
            "map": QgsUnitTypes.encodeUnit(map_canvas.mapUnits()),
            "area": QgsUnitTypes.encodeUnit(project.areaUnits()),
            "distance": QgsUnitTypes.encodeUnit(project.distanceUnits()),
            "factor": QgsUnitTypes.fromUnitToUnitFactor(map_canvas.mapUnits(), QgsUnitTypes.DistanceMeters) * 39.37007874
        }
        if project.readBoolEntry("PositionPrecision", "/Automatic")[0]:
            units["position_precision"] = project.readNumEntry("PositionPrecision", "/DecimalPlaces")[0]

        # if project.layerTreeRoot().hasCustomLayerOrder():
        #     layers_order = project.layerTreeRoot().customLayerOrder()
        # else:
        #     # layers_order = [tree_layer.layer() for tree_layer in reversed(QgsProject.instance().layerTreeRoot().findLayers())]
        #     layers_order = [tree_layer.layer() for tree_layer in QgsProject.instance().layerTreeRoot().findLayers()]

        data = {
            "file": QFileInfo(project.absoluteFilePath()).fileName(),
            # "directory": project.absolutePath(),
            "title": project.title() or project.readEntry("WMSServiceTitle", "/")[0],
            "layers": self.get_project_layers(skip_layers_with_error),
            "layers_order": [l.id() for l in project.layerTreeRoot().layerOrder()],
            "layers_tree": self.get_layers_tree(),
            "base_layers": self.get_background_layers(),
            "bookmarks": self.get_bookmarks(),
            "composer_templates": self.get_print_templates(),
            "projection": project_crs.authid(),
            "units": units,
            "scales": scales,
            "extent": format_extent(map_canvas.fullExtent().toRectF().getCoords(), project_crs),
            # "default_view_extent": view_settings.defaultViewExtent(),
            "projections": projections,
            "client_info": {
                "qgis_version": Qgis.QGIS_VERSION,
                "plugin_version": __metadata__["general"].get("version"),
                "platform": [platform.system(), platform.machine()],
                "directory": project.absolutePath()
            }
        }

        if project.isDirty():
            data["dirty"] = True

        with open(project.absoluteFilePath(), 'rb') as f:
            # maybe not necessary when project.isDirty()
            h = hashlib.sha1(f.read()).hexdigest()
            data["project_hash"] = h

        return data

    def get_settings(self):
        return QSettings(QSettings.IniFormat, QSettings.UserScope, "Gisquick", "gisquick")

    def show_settings(self):
        settings = self.get_settings()
        dialog_filename = os.path.join(self.plugin_dir, "ui", "settings.ui")
        dialog = PyQt5.uic.loadUi(dialog_filename)
        dialog.server_url.setText(settings.value("server_url", ""))
        dialog.username.setText(settings.value("username", ""))
        dialog.password.setText(settings.value("password", ""))

        dialog.show()
        res = dialog.exec_()
        if res == 1:
            settings.setValue("server_url", dialog.server_url.text().rstrip("/"))
            settings.setValue("username", dialog.username.text())
            settings.setValue("password", dialog.password.text())


    def on_project_change(self, *args):
        gisquick_ws.send("ProjectChanged")

    def on_project_closed(self, *args):
        def debounced():
            # filter events caused by switching between projects
            if not QgsProject.instance().absoluteFilePath():
                gisquick_ws.send("ProjectChanged")

        QTimer.singleShot(300, debounced)

    def toggle_tool(self, active):
        """Display dialog window for publishing current project.

        During a configuration process (wizard setup), plugin will hold actual metadata
        object in 'WebGisPlugin.metadata' property. If metadata from previous publishing
        still exist, they will be loaded and stored in 'WebGisPlugin.last_metadata' property.
        """
        if active:
            import json
            #meta = self.get_project_info(skip_layers_with_error=False)
            #layers = self.get_project_layers(skip_layers_with_error=False)
            # print(json.dumps(meta["layers_tree"]))
            # print(json.dumps(layers))
            # print(json.dumps(layers, indent=2))
        # return

        def callback(msg):
            msg_type = msg["type"]
            data = msg.get("data")
            project = QgsProject.instance()
            if not project.fileName():
                raise WsError("Project is not opened", 404)

            if msg_type == "ProjectInfo":
                if data:
                    skip_layers_with_error = data.get("skip_layers_with_error", False)
                return self.get_project_info(skip_layers_with_error=skip_layers_with_error)

            elif msg_type == "ProjectDirectory":
                return project.absolutePath()

            elif msg_type == "EnableLayersWFS":
                ids = [layer.id() for layer in project.mapLayers().values() if layer.type() == QgsMapLayerType.VectorLayer]
                project.writeEntry("WFSLayers", "/", ids)
                project.write()

            elif msg_type == "UpdateQgisProject":
                short_names = data.get("short_names", {})
                for lid, short_name in short_names.items():
                    layer = project.mapLayer(lid)
                    if layer:
                        layer.setShortName(short_name)
                project.write()

            else:
                raise ValueError("Unknown message type: %s" % msg_type)

        def on_connection_estabilished():
            # self.iface.messageBar().pushMessage("Gisquick", "plugin is connected to server: %s" % server_url, level=Qgis.Success)

            def open_browser():
                import webbrowser
                webbrowser.open(urljoin(server_url, '/user/'))

            widget = self.iface.messageBar().createMessage("Gisquick", "successfully connected to server: %s" % server_url)
            button = QPushButton(widget)
            button.setText("Open Browser")
            button.pressed.connect(open_browser)
            widget.layout().addWidget(button)
            self.iface.messageBar().pushWidget(widget, Qgis.Success)
            self.active_notification_widget = widget


        project = QgsProject.instance()
        if active:
            self.active_notification_widget = None
            settings = self.get_settings()
            server_url = settings.value("server_url")
            username = settings.value("username")
            password = settings.value("password")
            if not server_url or not username or not password:
                self.show_settings()
                server_url = settings.value("server_url")
                username = settings.value("username")
                password = settings.value("password")

            plugin_ver = __metadata__["general"].get("version")
            client_info = "GisquickPlugin/%s (%s %s; QGIS %s)" % (plugin_ver, platform.system(), platform.machine(), Qgis.QGIS_VERSION)

            class WebsocketServer(QThread):
                finished = QtCore.pyqtSignal(int)
                success = QtCore.pyqtSignal()

                def run(self):
                    # print("Starting WS", "server:", server_url, "user:", username)
                    def on_success():
                        self.success.emit()
                    res = gisquick_ws.start(server_url, username, password, client_info, callback, on_success)
                    self.finished.emit(res)

            def on_finished(res):
                self.ws = None
                if self.action.isChecked():
                    self.action.setChecked(False)
                if res != 0:
                    QMessageBox.warning(None, 'Warning', 'Failed to connect!')
                else:
                    if self.iface.messageBar().currentItem() == self.active_notification_widget:
                        self.iface.messageBar().popWidget(self.active_notification_widget)
                    self.active_notification_widget = None


            self.ws = WebsocketServer()
            self.ws.finished.connect(on_finished)
            self.ws.success.connect(on_connection_estabilished)
            r = self.ws.start()

            # project.isDirtyChanged.connect(self.on_project_change)
            project.readProject.connect(self.on_project_change)
            project.projectSaved.connect(self.on_project_change)
            project.cleared.connect(self.on_project_closed)
        else:
            project.readProject.disconnect(self.on_project_change)
            project.projectSaved.disconnect(self.on_project_change)
            project.cleared.disconnect(self.on_project_closed)
            gisquick_ws.stop()
