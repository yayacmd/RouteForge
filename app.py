#!/usr/bin/env python3
"""
Route Planner — a desktop tool for optimizing multi-vehicle delivery routes.

Solves a Capacitated Vehicle Routing Problem with Time Windows (CVRPTW) and
mid-route restocking using Google OR-Tools, geocoding/distance/directions via
LocationIQ, and an interactive Folium map. Originally built for fuel/oil
delivery; generalized here for any two-commodity delivery operation.

See README.md for setup. Requires a (free) LocationIQ API key.
"""

import sys
import io
import os
import uuid
import json
import time
import datetime as dt
from functools import partial
from email.mime.text import MIMEText
import smtplib
import urllib.request
import urllib.parse
from urllib.error import HTTPError

import numpy as np
import pandas as pd
import folium

from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp

from PyQt6 import QtWebEngineWidgets, QtCore, QtNetwork
from PyQt6.QtGui import (QIcon, QIntValidator, QDoubleValidator, QColor,
                         QPainter, QMovie, QPalette)
from PyQt6.QtCore import (Qt, QAbstractTableModel, QSize, QEasingCurve,
                          pyqtProperty, QPoint, QPropertyAnimation, QRect,
                          QObject, QTimer, QEvent, QMetaObject)
from PyQt6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QPushButton, QTabWidget, QWidget,
    QLineEdit, QHBoxLayout, QVBoxLayout, QToolBar, QComboBox, QTableView,
    QHeaderView, QCheckBox, QMessageBox, QDialog, QTimeEdit, QScrollArea,
    QSizePolicy, QDialogButtonBox, QTreeWidget, QFrame, QTreeWidgetItem
)

# ---------------------------------------------------------------------------
# Configuration — edit these to rebrand the tool for your own commodities.
# ---------------------------------------------------------------------------
APP_TITLE = "Route Planner"
PRODUCT_1_LABEL = "Product 1"          # e.g. "Diesel", "Cases", "Water"
PRODUCT_2_LABEL = "Product 2"          # e.g. "Gasoline", "Pallets"
UNIT_LABEL = "units"                   # e.g. "gallons", "liters", "boxes"
# Seconds spent restocking the vehicle at a depot (default 45 min).
DEPOT_SERVICE_TIME_SECONDS = 2700

APPDATA = "appdata"
API_KEY_FILE = "api_key.txt"

# US carrier email-to-MMS gateways for the optional "send routes" feature.
MMS_GATEWAYS = {
    "AT&T": "@mms.att.net",
    "Boost": "@myboostmobile.com",
    "Cricket": "@mms.mycricket.com",
    "Sprint": "@pm.sprint.com",
    "T-Mobile": "@tmomail.net",
    "Verizon": "@vzwpix.com",
    "Virgin": "@vmpix.com",
}

api_key = ""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def append_row(df, row):
    """Append a single dict row to a DataFrame (pandas 2.x compatible)."""
    new_df = pd.DataFrame([row])
    return new_df if df.empty else pd.concat([df, new_df], ignore_index=True)


def appdata_path(*parts):
    return os.path.join(APPDATA, *parts)


# ---------------------------------------------------------------------------
# LocationIQ network helpers
# ---------------------------------------------------------------------------
def get_travel_matrix(all_locations):
    """Build distance/duration matrices, chunking to respect API limits."""
    coordinates = all_locations['Longitude'].map(str) + ',' + all_locations['Latitude'].map(str)
    if len(coordinates) > 24:
        n = 12
        final = [coordinates[i * n:(i + 1) * n] for i in range((len(coordinates) + n - 1) // n)]
        distance_matrix = np.empty((0, len(coordinates)))
        duration_matrix = np.empty((0, len(coordinates)))
        for i in range(len(final)):
            response_duration = np.empty((len(final[i]), 0))
            response_distance = np.empty((len(final[i]), 0))
            for j in range(len(final)):
                request = 'https://us1.locationiq.com/v1/matrix/driving/'
                request = (request + ';'.join(final[i]) + ';' + ';'.join(final[j])
                           + '?key=' + api_key + '&annotations=distance,duration'
                           + '&sources=' + ';'.join(map(str, [*range(len(final[i]))]))
                           + '&destinations=' + ';'.join(map(str, [*range(len(final[i]), len(final[i]) + len(final[j]))])))
                json_result = urllib.request.urlopen(request).read()
                response = json.loads(json_result)
                response_duration = np.hstack((response_duration, response['durations']))
                response_distance = np.hstack((response_distance, response['distances']))
                time.sleep(0.5)
            distance_matrix = np.vstack((distance_matrix, response_distance))
            duration_matrix = np.vstack((duration_matrix, response_duration))
        return {'distance_matrix': distance_matrix, 'duration_matrix': duration_matrix}
    else:
        request = 'https://us1.locationiq.com/v1/matrix/driving/'
        request = request + ';'.join(coordinates) + '?key=' + api_key + '&annotations=distance,duration'
        json_result = urllib.request.urlopen(request).read()
        response = json.loads(json_result)
        return {'distance_matrix': response['distances'], 'duration_matrix': response['durations']}


def get_route(route):
    request = 'https://us1.locationiq.com/v1/directions/driving/'
    request = request + route + '?key=' + api_key + '&overview=full&geometries=geojson'
    json_result = urllib.request.urlopen(request).read()
    return json.loads(json_result)['routes'][0]['geometry']['coordinates']


def get_map(markers, steps, names):
    m = folium.Map(
        location=[route_vehicles['Start Latitude'].mean(), route_vehicles['Start Longitude'].mean()],
        zoom_start=10, tiles='cartodbpositron')

    colors = ['black', 'blue', 'cadetblue', 'darkblue', 'darkpurple', 'gray',
              'lightblue', 'lightgray', 'orange', 'pink', 'purple']
    depot_coords = []
    for i in range(len(depot_locations)):
        depot_coords.append([depot_locations['Latitude'][i], depot_locations['Longitude'][i]])

    truck = 1
    for step in steps:
        step = json.loads(step)
        vehicle_id = str(route_vehicles['Vehicle No.'][truck - 1])
        step = [pt[::-1] for pt in step]
        folium.PolyLine(step, color=colors[(truck - 1) % len(colors)], weight=5,
                        opacity=0.5, tooltip='Truck ' + vehicle_id).add_to(m)
        truck += 1

    truck = 1
    for marker in markers:
        pairs = marker.split("|")
        color = colors[(truck - 1) % len(colors)]
        stop = 1
        for i in range(1, len(pairs)):
            vehicle_id = str(route_vehicles['Vehicle No.'][truck - 1])
            location_name = str(names[truck - 1][stop])
            coords = [float(pairs[i].split(",")[0]), float(pairs[i].split(",")[1])]
            popup = "Truck " + vehicle_id + " Stop " + str(stop) + "\n" + location_name
            if coords in depot_coords:
                folium.Marker(coords, icon=folium.Icon(color='green', icon='warehouse', prefix='fa'),
                              tooltip='Restock Depot').add_to(m)
            else:
                folium.Marker(coords, icon=folium.Icon(color=color, icon='map-marker', prefix='fa'),
                              tooltip=popup).add_to(m)
            stop += 1
        truck += 1

    for idx, row in start_locations.iterrows():
        folium.Marker([row['Latitude'], row['Longitude']],
                      icon=folium.Icon(color='red', icon='home', prefix='fa'),
                      tooltip=row['Location Name']).add_to(m)
    return m


# ---------------------------------------------------------------------------
# OR-Tools model
# ---------------------------------------------------------------------------
def create_data_model(params):
    """Stores the data for the problem."""
    global day_start, all_locations, matrix_response
    max_time = params['max_time']
    fuel1_refill = -1 * route_vehicles['Product 1 Capacity'].astype(int).max()
    fuel2_refill = -1 * route_vehicles['Product 2 Capacity'].astype(int).max()
    stops = route_stops.reset_index()
    day_start = params['day_start']
    for i in range(len(route_stops)):
        starthour, startminute, startsec = map(int, stops['Delivery Window Start'][i].split(':'))
        endhour, endminute, endsec = map(int, stops['Delivery Window End'][i].split(':'))
        start = 60 * (starthour * 60 + startminute)
        end = 60 * (endhour * 60 + endminute)
        if start < day_start:
            start += 86400
            end += 86400
        elif end < day_start:
            end += 86400
        stops.at[i, 'Delivery Window Start'] = start - day_start
        stops.at[i, 'Delivery Window End'] = end - day_start

    depot_cols = ['Location Name', 'Location Address', 'Longitude', 'Latitude',
                  'Product 1 Delivery Amount', 'Product 2 Delivery Amount',
                  'Delivery Window Start', 'Delivery Window End']
    dup_depots = pd.DataFrame(columns=depot_cols)
    for _ in range(len(route_vehicles)):
        for i in range(len(depot_locations)):
            dup_depots = append_row(dup_depots, {
                'Location Name': depot_locations['Location Name'][i],
                'Location Address': depot_locations['Address'][i],
                'Longitude': depot_locations['Longitude'][i],
                'Latitude': depot_locations['Latitude'][i],
                'Product 1 Delivery Amount': fuel1_refill,
                'Product 2 Delivery Amount': fuel2_refill,
                'Delivery Window Start': min(stops['Delivery Window Start']),
                'Delivery Window End': max(stops['Delivery Window End'])})

    starts = pd.DataFrame(columns=depot_cols)
    for i in range(len(route_vehicles)):
        starts = append_row(starts, {
            'Location Name': route_vehicles['Start Location'][i],
            'Location Address': route_vehicles['Start Address'][i],
            'Longitude': route_vehicles['Start Longitude'][i],
            'Latitude': route_vehicles['Start Latitude'][i],
            'Product 1 Delivery Amount': 0,
            'Product 2 Delivery Amount': 0,
            'Delivery Window Start': 0,
            'Delivery Window End': max_time})
    for i in range(len(route_vehicles)):
        fuel1 = int(route_vehicles['Product 1 Capacity'][i]) - int(route_vehicles['Starting Product 1 Amount'][i])
        fuel2 = int(route_vehicles['Product 2 Capacity'][i]) - int(route_vehicles['Starting Product 2 Amount'][i])
        starts = append_row(starts, {
            'Location Name': route_vehicles['Start Location'][i],
            'Location Address': route_vehicles['Start Address'][i],
            'Longitude': route_vehicles['Start Longitude'][i],
            'Latitude': route_vehicles['Start Latitude'][i],
            'Product 1 Delivery Amount': fuel1,
            'Product 2 Delivery Amount': fuel2,
            'Delivery Window Start': 0,
            'Delivery Window End': max_time})

    all_locations = pd.concat([starts, dup_depots, stops], axis=0).reset_index(drop=True)
    matrix_response = get_travel_matrix(all_locations)
    time_windows = pd.concat([all_locations['Delivery Window Start'],
                              all_locations['Delivery Window End']], axis=1).values

    data = {}
    data['distance_matrix'] = matrix_response['distance_matrix']
    data['time_matrix'] = matrix_response['duration_matrix']
    data['time_windows'] = time_windows.tolist()
    data['num_locations'] = len(all_locations)
    data['demands1'] = all_locations['Product 1 Delivery Amount'].astype(int).tolist()
    data['demands2'] = all_locations['Product 2 Delivery Amount'].astype(int).tolist()
    data['vehicle_capacities1'] = route_vehicles['Product 1 Capacity'].astype(int).tolist()
    data['vehicle_capacities2'] = route_vehicles['Product 2 Capacity'].astype(int).tolist()
    data['vehicle_max_time'] = max_time
    data['num_vehicles'] = len(route_vehicles)
    data['time_per_demand_unit'] = 60 * 1 / params['service_rate']
    data['starts'] = [*range(len(route_vehicles), len(route_vehicles) * 2)]
    data['ends'] = [*range(0, len(route_vehicles))]
    data['depots'] = [*range(len(starts), len(starts) + len(dup_depots))]
    return data


def print_solution(data, manager, routing, solution):
    """Builds per-vehicle route tables, a summary string, and a map."""
    total_distance = 0
    total_load = 0
    total_time = 0
    map_markers = []
    route_steps = []
    map_locations = []
    route_solution = ''
    routes = {}
    for vehicle_id in range(data['num_vehicles']):
        df = pd.DataFrame(columns=['Location Name', 'Location Address',
                                   'Delivery Window Start', 'Delivery Window End',
                                   'Cumulative ' + PRODUCT_1_LABEL + ' Delivered',
                                   'Cumulative ' + PRODUCT_2_LABEL + ' Delivered'])
        index = routing.Start(vehicle_id)
        truck_id = 'Vehicle No. ' + str(route_vehicles['Vehicle No.'][vehicle_id])
        route_distance = 0
        route_load1 = 0
        route_load2 = 0
        end_loc = data['ends'][vehicle_id]
        route_coordinates = []
        map_coordinates = []
        map_location = []
        time_dimension = routing.GetDimensionOrDie('Time')
        start_load1 = int(route_vehicles['Product 1 Capacity'][vehicle_id]) - int(route_vehicles['Starting Product 1 Amount'][vehicle_id])
        start_load2 = int(route_vehicles['Product 2 Capacity'][vehicle_id]) - int(route_vehicles['Starting Product 2 Amount'][vehicle_id])
        while not routing.IsEnd(index):
            time_var = time_dimension.CumulVar(index)
            node_index = manager.IndexToNode(index)
            if node_index in data['depots']:
                location_name = str(all_locations['Location Name'][node_index]) + ' (Restock Depot)'
            else:
                route_load1 += data['demands1'][node_index]
                route_load2 += data['demands2'][node_index]
                location_name = str(all_locations['Location Name'][node_index])
            location_address = all_locations['Location Address'][node_index]
            window_start = dt.timedelta(seconds=solution.Min(time_var) + day_start)
            window_end = dt.timedelta(seconds=solution.Max(time_var) + day_start)
            df = append_row(df, {
                'Location Name': location_name,
                'Location Address': location_address,
                'Delivery Window Start': window_start,
                'Delivery Window End': window_end,
                'Cumulative ' + PRODUCT_1_LABEL + ' Delivered': route_load1 - start_load1,
                'Cumulative ' + PRODUCT_2_LABEL + ' Delivered': route_load2 - start_load2})
            previous_index = index
            index = solution.Value(routing.NextVar(index))
            route_distance += routing.GetArcCostForVehicle(previous_index, index, vehicle_id)
            route_coordinates.append(str(all_locations['Longitude'][node_index]) + ',' + str(all_locations['Latitude'][node_index]))
            map_coordinates.append(str(all_locations['Latitude'][node_index]) + ',' + str(all_locations['Longitude'][node_index]))
            map_location.append(str(all_locations['Location Name'][node_index]))
        time_var = time_dimension.CumulVar(index)
        return_name = str(all_locations['Location Name'][end_loc])
        return_address = str(all_locations['Location Address'][end_loc])
        df = append_row(df, {
            'Location Name': return_name,
            'Location Address': return_address,
            'Delivery Window Start': dt.timedelta(seconds=solution.Min(time_var) + day_start),
            'Delivery Window End': dt.timedelta(seconds=solution.Max(time_var) + day_start),
            'Cumulative ' + PRODUCT_1_LABEL + ' Delivered': route_load1 - start_load1,
            'Cumulative ' + PRODUCT_2_LABEL + ' Delivered': route_load2 - start_load2})
        total_fuel = route_load1 + route_load2 - start_load1 - start_load2
        routes[truck_id] = {
            'dataframe': df,
            'Total Delivered': total_fuel,
            'Total Distance': round(route_distance * 0.000621, 2),
            'Total Time': round(solution.Min(time_var) / 60, 0),
            'Coordinates': map_coordinates}

        total_distance += route_distance
        total_load += route_load1 + route_load2 - start_load1 - start_load2
        total_time += round(solution.Min(time_var) / 60, 0)
        route_steps.append(str(get_route(';'.join(route_coordinates) + ';'
                                          + str(route_vehicles['Start Longitude'][vehicle_id]) + ','
                                          + str(route_vehicles['Start Latitude'][vehicle_id]))))
        time.sleep(0.5)
        map_markers.append('|'.join(map_coordinates))
        map_locations.append(map_location)

    route_solution += 'Total distance of all routes: {} miles\t'.format(round(total_distance * 0.000621, 2))
    route_solution += 'Total load of all routes: {} {}\t'.format(total_load, UNIT_LABEL)
    route_solution += 'Total time of all routes: {} min'.format(total_time)
    route_map = get_map(map_markers, route_steps, map_locations)
    return routes, route_solution, route_map


def create_demand_evaluator1(data):
    _demands = data['demands1']

    def demand_evaluator(manager, from_node):
        return _demands[manager.IndexToNode(from_node)]
    return demand_evaluator


def create_demand_evaluator2(data):
    _demands = data['demands2']

    def demand_evaluator(manager, from_node):
        return _demands[manager.IndexToNode(from_node)]
    return demand_evaluator


def add_capacity_constraints(routing, manager, data, demand_evaluator_index,
                             capacities, dimension_name, drop_penalty):
    """Adds a capacity constraint for one commodity with depot reloading."""
    routing.AddDimensionWithVehicleCapacity(
        demand_evaluator_index, 10000, capacities, True, dimension_name)
    capacity_dimension = routing.GetDimensionOrDie(dimension_name)
    # Reloading nodes (depots) may be dropped with zero cost.
    for node in data['depots']:
        node_index = manager.NodeToIndex(node)
        routing.AddDisjunction([node_index], 0)
    # Regular delivery nodes may be dropped, but at a cost.
    for node in range(max(data['depots']) + 1, data['num_locations']):
        node_index = manager.NodeToIndex(node)
        capacity_dimension.SlackVar(node_index).SetValue(0)
        routing.AddDisjunction([node_index], drop_penalty)


def create_time_evaluator(data):
    """Creates callback to get total times between locations."""
    def service_time1(data, node):
        if node in data['starts']:
            return 0
        elif node in data['depots']:
            return DEPOT_SERVICE_TIME_SECONDS
        return abs(data['demands1'][node]) * data['time_per_demand_unit']

    def service_time2(data, node):
        if node in data['starts']:
            return 0
        elif node in data['depots']:
            return DEPOT_SERVICE_TIME_SECONDS
        return abs(data['demands2'][node]) * data['time_per_demand_unit']

    def travel_time(data, from_node, to_node):
        return 0 if from_node == to_node else data['time_matrix'][from_node][to_node]

    _total_time = {}
    for from_node in range(data['num_locations']):
        _total_time[from_node] = {}
        for to_node in range(data['num_locations']):
            if from_node == to_node:
                _total_time[from_node][to_node] = 0
            else:
                _total_time[from_node][to_node] = int(
                    service_time1(data, from_node) + service_time2(data, from_node)
                    + travel_time(data, from_node, to_node))

    def time_evaluator(manager, from_node, to_node):
        return _total_time[manager.IndexToNode(from_node)][manager.IndexToNode(to_node)]
    return time_evaluator


def add_time_window_constraints(routing, manager, data, time_evaluator):
    time = 'Time'
    max_time = data['vehicle_max_time']
    routing.AddDimension(time_evaluator, max_time, max_time, False, time)
    time_dimension = routing.GetDimensionOrDie(time)
    for location_idx, time_window in enumerate(data['time_windows']):
        if location_idx in range(0, max(data['depots'])):
            continue
        index = manager.NodeToIndex(location_idx)
        time_dimension.CumulVar(index).SetRange(time_window[0], time_window[1])
        routing.AddToAssignment(time_dimension.SlackVar(index))
    for vehicle_id in range(data['num_vehicles']):
        index = routing.Start(vehicle_id)
        time_dimension.CumulVar(index).SetRange(data['time_windows'][0][0], data['time_windows'][0][1])
        routing.AddToAssignment(time_dimension.SlackVar(index))


def solve_routes(params):
    """Solve the CVRPTW. Returns (routes, summary, map) or 'No Solution Found'."""
    data = create_data_model(params)
    manager = pywrapcp.RoutingIndexManager(len(data['distance_matrix']),
                                            data['num_vehicles'], data['starts'], data['ends'])
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        if from_node == to_node:
            return 0
        elif from_node in data['depots'] and to_node in data['depots']:
            return 1000000
        return data['distance_matrix'][from_node][to_node]

    transit_callback_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)

    demand_evaluator_index1 = routing.RegisterUnaryTransitCallback(
        partial(create_demand_evaluator1(data), manager))
    add_capacity_constraints(routing, manager, data, demand_evaluator_index1,
                             data['vehicle_capacities1'], PRODUCT_1_LABEL + ' Capacity', 100000)

    demand_evaluator_index2 = routing.RegisterUnaryTransitCallback(
        partial(create_demand_evaluator2(data), manager))
    add_capacity_constraints(routing, manager, data, demand_evaluator_index2,
                             data['vehicle_capacities2'], PRODUCT_2_LABEL + ' Capacity', 10000000)

    time_evaluator_index = routing.RegisterTransitCallback(
        partial(create_time_evaluator(data), manager))
    add_time_window_constraints(routing, manager, data, time_evaluator_index)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(1)

    solution = routing.SolveWithParameters(search_parameters)
    if solution:
        return print_solution(data, manager, routing, solution)
    return 'No Solution Found'


# ---------------------------------------------------------------------------
# Load persisted application data
# ---------------------------------------------------------------------------
delivery_locations = pd.read_json(appdata_path('delivery_locations.json'))
start_locations = pd.read_json(appdata_path('start_locations.json'))
depot_locations = pd.read_json(appdata_path('depot_locations.json'))
delivery_vehicles = pd.read_json(appdata_path('delivery_vehicles.json'))
route_vehicles = pd.read_json(appdata_path('route_vehicles.json'))
route_stops = pd.read_json(appdata_path('route_stops.json'))
drivers_list = pd.read_json(appdata_path('drivers_list.json'))

if os.path.exists(API_KEY_FILE):
    with open(API_KEY_FILE, 'r') as f:
        api_key = f.read().strip()


# ---------------------------------------------------------------------------
# Background worker (runs the solve off the GUI thread)
# ---------------------------------------------------------------------------
class RouteWorker(QtCore.QThread):
    finished_ok = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, params):
        super().__init__()
        self.params = params

    def run(self):
        try:
            self.finished_ok.emit(solve_routes(self.params))
        except Exception as exc:  # surfaced to the user in the GUI
            self.failed.emit(str(exc))


def clearLayout(layout):
    while layout.count():
        child = layout.takeAt(0)
        if child.widget() is not None:
            child.widget().deleteLater()
        elif child.layout() is not None:
            clearLayout(child.layout())


# ---------------------------------------------------------------------------
# Qt models / widgets
# ---------------------------------------------------------------------------
class PandasModel(QAbstractTableModel):
    def __init__(self, data, read_only_columns, save_location):
        super().__init__()
        self._data = data
        self.read_only_cols = read_only_columns
        self.save_location = save_location

    def rowCount(self, index):
        return self._data.shape[0]

    def columnCount(self, parent=None):
        return self._data.shape[1]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid():
            if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
                return str(self._data.iloc[index.row(), index.column()])

    def setData(self, index, value, role):
        if role == Qt.ItemDataRole.EditRole:
            self._data.iloc[index.row(), index.column()] = value
            if self.save_location:
                self._data.to_json(self.save_location)
            self.dataChanged.emit(index, index)
            return True
        return False

    def headerData(self, col, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self._data.columns[col]

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        if index.column() in self.read_only_cols:
            return base
        return base | Qt.ItemFlag.ItemIsEditable


class ToggleButton(QCheckBox):
    def __init__(self, width=70, bgColor="#777", circleColor="#DDD",
                 activeColor="#1168e4", animationCurve=QEasingCurve.Type.OutBounce):
        QCheckBox.__init__(self)
        self.setFixedSize(width, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._bg_color = bgColor
        self._circle_color = circleColor
        self._active_color = activeColor
        self._circle_position = 3
        self.animation = QPropertyAnimation(self, b"circle_position")
        self.animation.setEasingCurve(animationCurve)
        self.animation.setDuration(500)
        self.stateChanged.connect(self.start_transition)

    @pyqtProperty(int)
    def circle_position(self):
        return self._circle_position

    @circle_position.setter
    def circle_position(self, pos):
        self._circle_position = pos
        self.update()

    def start_transition(self, value):
        self.animation.setStartValue(self.circle_position)
        self.animation.setEndValue(self.width() - 18 if value else 3)
        self.animation.start()

    def hitButton(self, pos: QPoint):
        return self.contentsRect().contains(pos)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        rect = QRect(0, 0, self.width(), self.height())
        if not self.isChecked():
            p.setBrush(QColor(self._bg_color))
        else:
            p.setBrush(QColor(self._active_color))
        p.drawRoundedRect(0, 0, rect.width(), self.height(), self.height() / 2, self.height() / 2)
        p.setBrush(QColor(self._circle_color))
        p.drawEllipse(self._circle_position, 2, 16, 16)


class SuggestCompletion(QObject):
    finished = QtCore.pyqtSignal()

    def __init__(self, parent):
        QObject.__init__(self, parent)
        self._parent = parent
        self._editor = parent
        self._popup = QTreeWidget()
        self._popup.setWindowFlags(Qt.WindowType.Popup)
        self._popup.setFocusProxy(self._parent)
        self._popup.setMouseTracking(True)
        self._popup.setColumnCount(1)
        self._popup.setUniformRowHeights(True)
        self._popup.setRootIsDecorated(False)
        self._popup.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self._popup.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self._popup.setFrameStyle(QFrame.Shape.Box | QFrame.Shadow.Plain)
        self._popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._popup.header().hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(500)
        self._network_manager = QtNetwork.QNetworkAccessManager(self)
        self._popup.installEventFilter(self)
        self._popup.itemClicked.connect(self.done_completion)
        self._timer.timeout.connect(self.auto_suggest)
        self._editor.textEdited.connect(self._timer.start)
        self._network_manager.finished.connect(self.handle_network_data)

    def eventFilter(self, obj, event):
        if obj != self._popup:
            return False
        if event.type() == QEvent.Type.MouseButtonPress:
            self._popup.hide()
            self._editor.setFocus()
            return True
        if event.type() == QEvent.Type.KeyPress:
            consumed = False
            key = event.key()
            if key in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                self.done_completion()
                consumed = True
            elif key == Qt.Key.Key_Escape:
                self._editor.setFocus()
                self._popup.hide()
                consumed = True
            elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home,
                         Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                pass
            else:
                self._editor.setFocus()
                self._editor.event(event)
                self._popup.hide()
            return consumed
        return False

    def show_completion(self, choices):
        if not choices:
            return
        palette = self._editor.palette()
        color = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
        self._popup.setUpdatesEnabled(False)
        self._popup.clear()
        for choice in choices:
            item = QTreeWidgetItem(self._popup)
            item.setText(0, choice['label'])
            item.setData(0, Qt.ItemDataRole.UserRole, choice['lon'])
            item.setData(0, Qt.ItemDataRole.UserRole + 1, choice['lat'])
            item.setForeground(0, color)
        self._popup.setCurrentItem(self._popup.topLevelItem(0))
        self._popup.resizeColumnToContents(0)
        self._popup.setUpdatesEnabled(True)
        self._popup.move(self._editor.mapToGlobal(QPoint(0, self._editor.height())))
        self._popup.setFocus()
        self._popup.show()

    def done_completion(self):
        self._timer.stop()
        self._popup.hide()
        self._editor.setFocus()
        item = self._popup.currentItem()
        if item:
            self._editor.setText(item.text(0))
            self._editor.setProperty('lon', item.data(0, Qt.ItemDataRole.UserRole))
            self._editor.setProperty('lat', item.data(0, Qt.ItemDataRole.UserRole + 1))
            QMetaObject.invokeMethod(self._editor, 'returnPressed')
        self.finished.emit()

    def auto_suggest(self):
        text = self._editor.text()
        url = QtCore.QUrl("https://api.locationiq.com/v1/search.php?")
        query = QtCore.QUrlQuery()
        query.addQueryItem("key", api_key)
        query.addQueryItem("q", text.replace(' ', '+'))
        query.addQueryItem("countrycodes", "us")
        query.addQueryItem("accept-language", "en")
        query.addQueryItem("limit", "10")
        query.addQueryItem("format", "json")
        url.setQuery(query)
        self._network_manager.get(QtNetwork.QNetworkRequest(url))

    def prevent_suggest(self):
        self._timer.stop()

    def handle_network_data(self, network_reply):
        choices = []
        if network_reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError:
            data = json.loads(network_reply.readAll().data())
            for prediction in data:
                choices.append({
                    'label': prediction['display_name'],
                    'lon': prediction['lon'],
                    'lat': prediction['lat']})
            self.show_completion(choices)
        else:
            if network_reply.error() != QtNetwork.QNetworkReply.NetworkError.ContentNotFoundError:
                QMessageBox.warning(self._parent, 'Error',
                                    str(network_reply.error().name) + ' Error Code: '
                                    + str(network_reply.error().value))
        network_reply.deleteLater()


class SearchBox(QLineEdit):
    def __init__(self, parent=None):
        super(SearchBox, self).__init__(parent)
        self._completer = SuggestCompletion(self)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        if os.path.exists('fast-delivery.png'):
            self.setWindowIcon(QIcon('fast-delivery.png'))
        self.setMinimumWidth(1000)

        toolbar = QToolBar("My main toolbar")
        self.api = QLineEdit()
        self.api.setPlaceholderText('LocationIQ API Key')
        self.api.setMaximumWidth(300)
        if api_key != '':
            self.api.setText(api_key)
        self.saveapi = QPushButton('Save')
        self.saveapi.clicked.connect(self.save_api)
        toolbar.addWidget(self.api)
        toolbar.addWidget(self.saveapi)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        light_theme = QLabel('Light Mode')
        self.theme = ToggleButton()
        self.theme.clicked.connect(self.toggle_theme)
        dark_theme = QLabel('Dark Mode')
        toolbar.addWidget(spacer)
        toolbar.addWidget(light_theme)
        toolbar.addWidget(self.theme)
        toolbar.addWidget(dark_theme)
        self.addToolBar(toolbar)

        tabs = QTabWidget()
        tabs.setMovable(True)

        # ---- Delivery Locations tab ----
        location_label = QLabel('Delivery Locations')
        location_label.setProperty('title', True)
        self.delivery_location = SearchBox()
        self.delivery_location.setPlaceholderText('Enter Delivery Address')
        self.delivery_location._completer.finished.connect(self.location_search)
        self.search_results = QVBoxLayout()

        self.table1 = QTableView()
        self.model = PandasModel(delivery_locations, [0, 2, 3, 4], appdata_path('delivery_locations.json'))
        self.table1.setModel(self.model)
        self.table1_layout = QVBoxLayout()
        self.table1_layout.addWidget(self.table1)
        self.table1.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for c in (0, 2, 3):
            self.table1.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self.table1.horizontalHeader().setMinimumHeight(40)
        self.table1.hideColumn(0)

        delete_location = QPushButton('Delete Selected Location(s)')
        delete_location.clicked.connect(self.delete_1)
        delete_all_location = QPushButton('Delete All Locations')
        delete_all_location.clicked.connect(self.delete_2)
        location_row_4 = QHBoxLayout()
        location_row_4.addWidget(delete_location)
        location_row_4.addWidget(delete_all_location)

        self.location_col = QVBoxLayout()
        self.location_col.addWidget(location_label)
        self.location_col.addWidget(self.delivery_location)
        self.location_col.addLayout(self.search_results)
        self.location_col.addLayout(self.table1_layout)
        self.location_col.addLayout(location_row_4)
        location_widget = QWidget()
        location_widget.setLayout(self.location_col)
        tabs.addTab(location_widget, 'Delivery Locations')

        # ---- Start & Depot Locations tab ----
        depot_label = QLabel('Start & Depot Locations')
        depot_label.setProperty('title', True)
        self.depot_location = SearchBox()
        self.depot_location.setPlaceholderText('Enter Location Address')
        self.depot_location._completer.finished.connect(self.depot_search)
        self.depot_search_results = QVBoxLayout()

        starts_label = QLabel('Vehicle Start Locations')
        self.table2 = QTableView()
        self.model2 = PandasModel(start_locations, [0, 2, 3, 4], appdata_path('start_locations.json'))
        self.table2.setModel(self.model2)
        self.table2_layout = QVBoxLayout()
        self.table2_layout.addWidget(self.table2)
        self.table2.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for c in (0, 2, 3):
            self.table2.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self.table2.horizontalHeader().setMinimumHeight(40)
        self.table2.hideColumn(0)

        depots_label = QLabel('Restock Depot Locations')
        self.table3 = QTableView()
        self.model3 = PandasModel(depot_locations, [1, 2, 3], appdata_path('depot_locations.json'))
        self.table3.setModel(self.model3)
        self.table3_layout = QVBoxLayout()
        self.table3_layout.addWidget(self.table3)
        self.table3.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for c in (0, 2, 3):
            self.table3.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        self.table3.horizontalHeader().setMinimumHeight(40)

        delete_start_location = QPushButton('Delete Selected Location(s)')
        delete_start_location.clicked.connect(self.delete_3)
        delete_allstart_location = QPushButton('Delete All Locations')
        delete_allstart_location.clicked.connect(self.delete_4)
        delete_depot_location = QPushButton('Delete Selected Location(s)')
        delete_depot_location.clicked.connect(self.delete_5)
        delete_alldepot_location = QPushButton('Delete All Locations')
        delete_alldepot_location.clicked.connect(self.delete_6)
        depot_row_3 = QHBoxLayout()
        depot_row_3.addWidget(delete_start_location)
        depot_row_3.addWidget(delete_allstart_location)
        depot_row_5 = QHBoxLayout()
        depot_row_5.addWidget(delete_depot_location)
        depot_row_5.addWidget(delete_alldepot_location)

        self.depot_col = QVBoxLayout()
        self.depot_col.addWidget(depot_label)
        self.depot_col.addWidget(self.depot_location)
        self.depot_col.addLayout(self.depot_search_results)
        self.depot_col.addWidget(starts_label)
        self.depot_col.addLayout(self.table2_layout)
        self.depot_col.addLayout(depot_row_3)
        self.depot_col.addWidget(depots_label)
        self.depot_col.addLayout(self.table3_layout)
        self.depot_col.addLayout(depot_row_5)
        depot_widget = QWidget()
        depot_widget.setLayout(self.depot_col)
        tabs.addTab(depot_widget, 'Start and Depot Locations')

        # ---- Vehicles tab ----
        vehicle_label = QLabel('Delivery Vehicles')
        vehicle_label.setProperty('title', True)
        self.vehicle_name = QLineEdit()
        self.vehicle_name.setPlaceholderText('Vehicle No.')
        self.vehicle_start = QComboBox()
        self.vehicle_start.addItems(start_locations['Location Name'])
        capacity_label = QLabel('Vehicle\nCapacities')
        capacity_label.setProperty('title', True)
        self.vehicle_fuel1 = QLineEdit(self)
        self.vehicle_fuel1.setPlaceholderText(PRODUCT_1_LABEL + ' Capacity (' + UNIT_LABEL + ')')
        self.vehicle_fuel1.setValidator(QIntValidator())
        self.vehicle_fuel2 = QLineEdit(self)
        self.vehicle_fuel2.setPlaceholderText(PRODUCT_2_LABEL + ' Capacity (' + UNIT_LABEL + ')')
        self.vehicle_fuel2.setValidator(QIntValidator())
        add_vehicle = QPushButton('Add Vehicle')
        add_vehicle.clicked.connect(self.vehicle_add)

        vehicle_col_1 = QVBoxLayout()
        vehicle_col_1.addWidget(vehicle_label)
        vehicle_col_1.addWidget(QLabel('Vehicle No.:'))
        vehicle_col_1.addWidget(self.vehicle_name)
        vehicle_col_1.addWidget(QLabel('Vehicle Starting Location:'))
        vehicle_col_1.addWidget(self.vehicle_start)
        vehicle_col_1.addWidget(capacity_label)
        vehicle_col_1.addWidget(QLabel('Vehicle Capacity for ' + PRODUCT_1_LABEL + ':'))
        vehicle_col_1.addWidget(self.vehicle_fuel1)
        vehicle_col_1.addWidget(QLabel('Vehicle Capacity for ' + PRODUCT_2_LABEL + ':'))
        vehicle_col_1.addWidget(self.vehicle_fuel2)
        vehicle_col_1.addWidget(add_vehicle)
        vehicle_col_1.addStretch()
        vehicle_col_1_widget = QWidget()
        vehicle_col_1_widget.setLayout(vehicle_col_1)
        vehicle_col_1_widget.setFixedWidth(300)

        self.table4 = QTableView()
        self.model4 = PandasModel(delivery_vehicles, [0, 2, 3, 4, 5, 6], appdata_path('delivery_vehicles.json'))
        self.table4.setModel(self.model4)
        self.table4_layout = QVBoxLayout()
        self.table4_layout.addWidget(self.table4)
        self.table4.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table4.horizontalHeader().setMinimumHeight(40)
        self.table4.hideColumn(0)
        self.table4.hideColumn(2)

        delete_vehicle = QPushButton('Delete Selected Vehicle(s)')
        delete_vehicle.clicked.connect(self.delete_7)
        delete_all_vehicle = QPushButton('Delete All Vehicles')
        delete_all_vehicle.clicked.connect(self.delete_8)
        vehicle_row_del = QHBoxLayout()
        vehicle_row_del.addWidget(delete_vehicle)
        vehicle_row_del.addWidget(delete_all_vehicle)
        vehicle_col_2 = QVBoxLayout()
        vehicle_col_2.addLayout(self.table4_layout)
        vehicle_col_2.addLayout(vehicle_row_del)
        vehicle_row = QHBoxLayout()
        vehicle_row.addWidget(vehicle_col_1_widget)
        vehicle_row.addLayout(vehicle_col_2)
        vehicle_widget = QWidget()
        vehicle_widget.setLayout(vehicle_row)
        tabs.addTab(vehicle_widget, 'Vehicles')

        # ---- Route Stops tab ----
        stops_label = QLabel('Route Stops')
        stops_label.setProperty('title', True)
        self.save_template = QPushButton('Save as Template')
        self.save_template.clicked.connect(self.save_route_template)
        self.load_template = QPushButton('Open Templates')
        self.load_template.clicked.connect(self.load_route_template)
        self.template_select = QComboBox()
        self.template_select.addItems(os.listdir(appdata_path('route_templates')))
        self.template_select.hide()
        self.delete_template = QPushButton('Delete Selected Template')
        self.delete_template.clicked.connect(self.delete_route_template)
        self.delete_template.hide()
        stop_row = QHBoxLayout()
        stop_row.addWidget(stops_label)
        stop_row.addStretch()
        stop_row.addWidget(self.save_template)
        stop_row.addWidget(self.load_template)
        stop_row.addWidget(self.template_select)
        stop_row.addWidget(self.delete_template)

        self.stop = QComboBox()
        self.stop.addItems(delivery_locations['Location Name'])
        self.fuel1_amt = QLineEdit()
        self.fuel1_amt.setPlaceholderText(PRODUCT_1_LABEL + ' Delivery Amount')
        self.fuel1_amt.setValidator(QIntValidator())
        self.fuel2_amt = QLineEdit()
        self.fuel2_amt.setPlaceholderText(PRODUCT_2_LABEL + ' Delivery Amount')
        self.fuel2_amt.setValidator(QIntValidator())
        self.window_start = QTimeEdit()
        self.window_end = QTimeEdit()
        add_stop = QPushButton('Add Stop')
        add_stop.clicked.connect(self.add_route_stop)

        self.table5 = QTableView()
        self.model5 = PandasModel(route_stops, [0, 1, 2, 3, 4], appdata_path('route_stops.json'))
        self.table5.setModel(self.model5)
        self.table5_layout = QVBoxLayout()
        self.table5_layout.addWidget(self.table5)
        self.table5.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table5.horizontalHeader().setMinimumHeight(40)
        self.table5.hideColumn(0)

        stop_row1 = QHBoxLayout()
        stop_row1.addWidget(QLabel('Delivery Location:'))
        stop_row1.addWidget(self.stop)
        stop_row1.addWidget(self.fuel1_amt)
        stop_row1.addWidget(self.fuel2_amt)
        stop_row1.addWidget(QLabel('Delivery Window Start:'))
        stop_row1.addWidget(self.window_start)
        stop_row1.addWidget(QLabel('Delivery Window End:'))
        stop_row1.addWidget(self.window_end)
        stop_row1.addWidget(add_stop)

        stop_del_stop = QPushButton('Remove Selected Stop(s)')
        stop_del_stop.clicked.connect(self.delete_9)
        stop_del_allstop = QPushButton('Remove All Stops')
        stop_del_allstop.clicked.connect(self.delete_10)
        stop_del_row1 = QHBoxLayout()
        stop_del_row1.addWidget(stop_del_stop)
        stop_del_row1.addWidget(stop_del_allstop)

        self.route_vehicle = QComboBox()
        self.route_vehicle.addItems(map(str, delivery_vehicles['Vehicle No.']))
        self.fuel1_start = QLineEdit()
        self.fuel1_start.setPlaceholderText(PRODUCT_1_LABEL + ' Start Amount')
        self.fuel1_start.setValidator(QIntValidator())
        self.fuel2_start = QLineEdit()
        self.fuel2_start.setPlaceholderText(PRODUCT_2_LABEL + ' Start Amount')
        self.fuel2_start.setValidator(QIntValidator())
        add_route_vehicle = QPushButton('Add Vehicle to Route')
        add_route_vehicle.clicked.connect(self.add_vehicle_route)

        self.table6 = QTableView()
        self.model6 = PandasModel(route_vehicles, [0, 1, 2, 3, 4, 5, 6], appdata_path('route_vehicles.json'))
        self.table6.setModel(self.model6)
        self.table6_layout = QVBoxLayout()
        self.table6_layout.addWidget(self.table6)
        self.table6.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table6.horizontalHeader().setMinimumHeight(40)
        self.table6.hideColumn(0)
        self.table6.hideColumn(2)

        stop_row2 = QHBoxLayout()
        stop_row2.addWidget(QLabel('Delivery Vehicle:'))
        stop_row2.addWidget(self.route_vehicle)
        stop_row2.addWidget(self.fuel1_start)
        stop_row2.addWidget(self.fuel2_start)
        stop_row2.addWidget(add_route_vehicle)

        stop_del_vehicle = QPushButton('Remove Selected Vehicle(s)')
        stop_del_vehicle.clicked.connect(self.delete_11)
        stop_del_allvehicle = QPushButton('Remove All Vehicles')
        stop_del_allvehicle.clicked.connect(self.delete_12)
        stop_del_row2 = QHBoxLayout()
        stop_del_row2.addWidget(stop_del_vehicle)
        stop_del_row2.addWidget(stop_del_allvehicle)

        stops_col = QVBoxLayout()
        stops_col.addLayout(stop_row)
        stops_col.addLayout(stop_row1)
        stops_col.addLayout(self.table5_layout)
        stops_col.addLayout(stop_del_row1)
        stops_col.addLayout(stop_row2)
        stops_col.addLayout(self.table6_layout)
        stops_col.addLayout(stop_del_row2)
        stops_widget = QWidget()
        stops_widget.setLayout(stops_col)
        tabs.addTab(stops_widget, 'Route Stops')

        # ---- Drivers tab ----
        drivers_label = QLabel('Vehicle Drivers')
        drivers_label.setProperty('title', True)
        self.driver_first = QLineEdit()
        self.driver_first.setPlaceholderText('Driver First Name')
        self.driver_last = QLineEdit()
        self.driver_last.setPlaceholderText('Driver Last Name')
        self.driver_contact = QLineEdit()
        self.driver_contact.setPlaceholderText('Driver Cell Number')
        self.driver_contact_provider = QComboBox()
        self.driver_contact_provider.addItems(list(MMS_GATEWAYS.keys()))
        self.add_driver_button = QPushButton('Add Driver')
        self.add_driver_button.clicked.connect(self.add_driver)
        drivers_row_1 = QHBoxLayout()
        drivers_row_1.addWidget(self.driver_first)
        drivers_row_1.addWidget(self.driver_last)
        drivers_row_1.addWidget(self.driver_contact)
        drivers_row_1.addWidget(QLabel('Driver Cell Provider:'))
        drivers_row_1.addWidget(self.driver_contact_provider)
        drivers_row_1.addWidget(self.add_driver_button)

        self.table7 = QTableView()
        self.model7 = PandasModel(drivers_list, [], appdata_path('drivers_list.json'))
        self.table7.setModel(self.model7)
        self.table7_layout = QVBoxLayout()
        self.table7_layout.addWidget(self.table7)
        self.table7.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table7.horizontalHeader().setMinimumHeight(40)

        stop_del_driver = QPushButton('Remove Selected Driver(s)')
        stop_del_driver.clicked.connect(self.delete_13)
        stop_del_alldriver = QPushButton('Remove All Drivers')
        stop_del_alldriver.clicked.connect(self.delete_14)
        stop_del_row = QHBoxLayout()
        stop_del_row.addWidget(stop_del_driver)
        stop_del_row.addWidget(stop_del_alldriver)

        drivers_col = QVBoxLayout()
        drivers_col.addWidget(drivers_label)
        drivers_col.addLayout(drivers_row_1)
        drivers_col.addLayout(self.table7_layout)
        drivers_col.addLayout(stop_del_row)
        drivers_widget = QWidget()
        drivers_widget.setLayout(drivers_col)
        tabs.addTab(drivers_widget, 'Drivers')

        # ---- Build Routes tab ----
        planner_label = QLabel('Route Planning')
        planner_label.setProperty('title', True)
        self.day_start = QTimeEdit()
        self.fuel_rate = QLineEdit()
        self.fuel_rate.setPlaceholderText('service rate (' + UNIT_LABEL + '/min)')
        self.fuel_rate.setValidator(QDoubleValidator())
        self.max_shift = QLineEdit()
        self.max_shift.setPlaceholderText('Max Shift (hrs)')
        self.max_shift.setValidator(QDoubleValidator())
        build_route = QPushButton('Run Routing Tool')
        build_route.clicked.connect(self.run_algorithm)
        planner_row = QHBoxLayout()
        planner_row.addWidget(QLabel('Work Day/Shift Start Time:'))
        planner_row.addWidget(self.day_start)
        planner_row.addWidget(QLabel('Service / Loading Rate:'))
        planner_row.addWidget(self.fuel_rate)
        planner_row.addWidget(QLabel('Maximum Shift Length:'))
        planner_row.addWidget(self.max_shift)
        planner_row.addWidget(build_route)

        self.output_layout = QVBoxLayout()
        planner_col = QVBoxLayout()
        planner_col.addWidget(planner_label)
        planner_col.addLayout(planner_row)
        planner_col.addLayout(self.output_layout)
        planner_col.addStretch()
        planner_widget = QWidget()
        planner_widget.setLayout(planner_col)
        tabs.addTab(planner_widget, 'Build Routes')

        self.setCentralWidget(tabs)

        if os.path.exists('style.txt'):
            with open('style.txt', 'r') as fh:
                self.setStyleSheet(fh.read())

        self.table1.model().dataChanged.connect(self.update_route_stops_combo_box)
        self.table2.model().dataChanged.connect(self.update_route_start_combo_box)
        self.table4.model().dataChanged.connect(self.update_route_vehicle_combo_box)

    # ---- API key ----
    def save_api(self):
        global api_key
        api_key = self.api.text().strip()
        with open(API_KEY_FILE, 'w') as f:
            f.write(api_key)

    # ---- Combo-box refreshers (kept simple; just resync from source tables) ----
    def update_route_stops_combo_box(self, index):
        self.stop.clear()
        self.stop.addItems(delivery_locations['Location Name'])

    def update_route_start_combo_box(self, index):
        self.vehicle_start.clear()
        self.vehicle_start.addItems(start_locations['Location Name'])

    def update_route_vehicle_combo_box(self, index):
        self.route_vehicle.clear()
        self.route_vehicle.addItems(map(str, delivery_vehicles['Vehicle No.']))

    # ---- Delivery locations ----
    def location_search(self):
        clearLayout(self.search_results)
        self.location_name = QLineEdit()
        self.location_name.setPlaceholderText('Location Name')
        add_location = QPushButton('Add Location')
        add_location.clicked.connect(self.add_delivery_location)
        search_row = QHBoxLayout()
        search_row.addWidget(self.location_name)
        search_row.addWidget(add_location)
        self.search_results.addLayout(search_row)

    def add_delivery_location(self):
        global delivery_locations
        location_name = self.location_name.text()
        address = self.delivery_location.text()
        lon = self.delivery_location.property('lon')
        lat = self.delivery_location.property('lat')
        uid = str(uuid.uuid4())
        while uid in delivery_locations['Location ID'].values:
            uid = str(uuid.uuid4())
        if location_name == '':
            QMessageBox.warning(self, 'Error', 'Location Name is Missing')
            return
        if len(delivery_locations) > 0 and delivery_locations['Location Name'].str.contains(location_name).any():
            QMessageBox.warning(self, 'Error', 'Delivery Location Name Already In Use')
            return
        delivery_locations = append_row(delivery_locations, {
            'Location ID': uid, 'Location Name': location_name, 'Address': address,
            'Longitude': lon, 'Latitude': lat})
        delivery_locations.to_json(appdata_path('delivery_locations.json'))
        self.stop.clear()
        self.stop.addItems(delivery_locations['Location Name'])
        self.table1.model()._data = delivery_locations
        self.table1.model().layoutChanged.emit()
        self.location_name.clear()
        self.delivery_location.clear()

    def delete_1(self):
        global delivery_locations
        rows = sorted(set(index.row() for index in self.table1.selectedIndexes()))
        delivery_locations = delivery_locations.drop(delivery_locations.index[rows]).reset_index(drop=True)
        delivery_locations.to_json(appdata_path('delivery_locations.json'))
        self.stop.clear()
        self.stop.addItems(delivery_locations['Location Name'])
        self.table1.model()._data = delivery_locations
        self.table1.model().layoutChanged.emit()

    def delete_2(self):
        global delivery_locations
        delivery_locations = delivery_locations[0:0]
        delivery_locations.to_json(appdata_path('delivery_locations.json'))
        self.stop.clear()
        self.table1.model()._data = delivery_locations
        self.table1.model().layoutChanged.emit()

    def delete_3(self):
        global start_locations
        rows = sorted(set(index.row() for index in self.table2.selectedIndexes()))
        start_locations = start_locations.drop(start_locations.index[rows]).reset_index(drop=True)
        start_locations.to_json(appdata_path('start_locations.json'))
        self.vehicle_start.clear()
        self.vehicle_start.addItems(start_locations['Location Name'])
        self.table2.model()._data = start_locations
        self.table2.model().layoutChanged.emit()

    def delete_4(self):
        global start_locations
        start_locations = start_locations[0:0]
        start_locations.to_json(appdata_path('start_locations.json'))
        self.vehicle_start.clear()
        self.table2.model()._data = start_locations
        self.table2.model().layoutChanged.emit()

    def delete_5(self):
        global depot_locations
        rows = sorted(set(index.row() for index in self.table3.selectedIndexes()))
        depot_locations = depot_locations.drop(depot_locations.index[rows]).reset_index(drop=True)
        depot_locations.to_json(appdata_path('depot_locations.json'))
        self.table3.model()._data = depot_locations
        self.table3.model().layoutChanged.emit()

    def delete_6(self):
        global depot_locations
        depot_locations = depot_locations[0:0]
        depot_locations.to_json(appdata_path('depot_locations.json'))
        self.table3.model()._data = depot_locations
        self.table3.model().layoutChanged.emit()

    def delete_7(self):
        global delivery_vehicles
        rows = sorted(set(index.row() for index in self.table4.selectedIndexes()))
        delivery_vehicles = delivery_vehicles.drop(delivery_vehicles.index[rows]).reset_index(drop=True)
        delivery_vehicles.to_json(appdata_path('delivery_vehicles.json'))
        self.route_vehicle.clear()
        self.route_vehicle.addItems(map(str, delivery_vehicles['Vehicle No.']))
        self.table4.model()._data = delivery_vehicles
        self.table4.model().layoutChanged.emit()

    def delete_8(self):
        global delivery_vehicles
        delivery_vehicles = delivery_vehicles[0:0]
        delivery_vehicles.to_json(appdata_path('delivery_vehicles.json'))
        self.route_vehicle.clear()
        self.table4.model()._data = delivery_vehicles
        self.table4.model().layoutChanged.emit()

    def delete_9(self):
        global route_stops
        rows = sorted(set(index.row() for index in self.table5.selectedIndexes()))
        route_stops = route_stops.drop(route_stops.index[rows]).reset_index(drop=True)
        route_stops.to_json(appdata_path('route_stops.json'))
        self.table5.model()._data = route_stops
        self.table5.model().layoutChanged.emit()

    def delete_10(self):
        global route_stops
        route_stops = route_stops[0:0]
        route_stops.to_json(appdata_path('route_stops.json'))
        self.table5.model()._data = route_stops
        self.table5.model().layoutChanged.emit()

    def delete_11(self):
        global route_vehicles
        rows = sorted(set(index.row() for index in self.table6.selectedIndexes()))
        route_vehicles = route_vehicles.drop(route_vehicles.index[rows]).reset_index(drop=True)
        route_vehicles.to_json(appdata_path('route_vehicles.json'))
        self.table6.model()._data = route_vehicles
        self.table6.model().layoutChanged.emit()

    def delete_12(self):
        global route_vehicles
        route_vehicles = route_vehicles[0:0]
        route_vehicles.to_json(appdata_path('route_vehicles.json'))
        self.table6.model()._data = route_vehicles
        self.table6.model().layoutChanged.emit()

    def delete_13(self):
        global drivers_list
        rows = sorted(set(index.row() for index in self.table7.selectedIndexes()))
        drivers_list = drivers_list.drop(drivers_list.index[rows]).reset_index(drop=True)
        drivers_list.to_json(appdata_path('drivers_list.json'))
        self.table7.model()._data = drivers_list
        self.table7.model().layoutChanged.emit()

    def delete_14(self):
        global drivers_list
        drivers_list = drivers_list[0:0]
        drivers_list.to_json(appdata_path('drivers_list.json'))
        self.table7.model()._data = drivers_list
        self.table7.model().layoutChanged.emit()

    # ---- Start / depot locations ----
    def depot_search(self):
        clearLayout(self.depot_search_results)
        self.depot_location_name = QLineEdit()
        self.depot_location_name.setPlaceholderText('Location Name')
        depot_add_location = QPushButton('Add Location')
        depot_add_location.clicked.connect(self.add_depot_location)
        self.start = QCheckBox('Vehicle Start')
        self.depot = QCheckBox('Restock Depot')
        depot_search_row = QHBoxLayout()
        depot_search_row.addWidget(self.depot_location_name)
        depot_search_row.addWidget(self.start)
        depot_search_row.addWidget(self.depot)
        depot_search_row.addWidget(depot_add_location)
        self.depot_search_results.addLayout(depot_search_row)

    def add_depot_location(self):
        global start_locations, depot_locations
        location_name = self.depot_location_name.text()
        address = self.depot_location.text()
        lon = self.depot_location.property('lon')
        lat = self.depot_location.property('lat')
        uid = str(uuid.uuid4())
        while uid in start_locations['Location ID'].values:
            uid = str(uuid.uuid4())
        if location_name == '':
            QMessageBox.warning(self, 'Error', 'Location Name is Missing')
            return
        if self.start.isChecked():
            if len(start_locations) > 0 and start_locations['Location Name'].str.contains(location_name).any():
                QMessageBox.warning(self, 'Error', 'Start Location Name Already In Use')
            else:
                start_locations = append_row(start_locations, {
                    'Location ID': uid, 'Location Name': location_name, 'Address': address,
                    'Longitude': lon, 'Latitude': lat})
                start_locations.to_json(appdata_path('start_locations.json'))
                self.vehicle_start.clear()
                self.vehicle_start.addItems(start_locations['Location Name'])
                self.table2.model()._data = start_locations
                self.table2.model().layoutChanged.emit()
        if self.depot.isChecked():
            if len(depot_locations) > 0 and depot_locations['Location Name'].str.contains(location_name).any():
                QMessageBox.warning(self, 'Error', 'Depot Location Name Already In Use')
            else:
                depot_locations = append_row(depot_locations, {
                    'Location Name': location_name, 'Address': address,
                    'Longitude': lon, 'Latitude': lat})
                depot_locations.to_json(appdata_path('depot_locations.json'))
                self.table3.model()._data = depot_locations
                self.table3.model().layoutChanged.emit()

    # ---- Vehicles ----
    def vehicle_add(self):
        global delivery_vehicles
        name = str(self.vehicle_name.text())
        start = self.vehicle_start.currentIndex()
        fuel1 = self.vehicle_fuel1.text()
        fuel2 = self.vehicle_fuel2.text()
        uid = str(uuid.uuid4())
        while uid in delivery_vehicles['Vehicle ID'].values:
            uid = str(uuid.uuid4())
        if name == '':
            QMessageBox.warning(self, 'Error', 'Vehicle No. is Missing')
            return
        if fuel1 == '':
            QMessageBox.warning(self, 'Error', PRODUCT_1_LABEL + ' capacity is Missing')
            return
        if fuel2 == '':
            QMessageBox.warning(self, 'Error', PRODUCT_2_LABEL + ' capacity is Missing')
            return
        delivery_vehicles = append_row(delivery_vehicles, {
            'Vehicle ID': uid, 'Vehicle No.': str(name),
            'Start Location ID': start_locations['Location ID'][start],
            'Start Location': start_locations['Location Name'][start],
            'Start Address': start_locations['Address'][start],
            'Start Longitude': start_locations['Longitude'][start],
            'Start Latitude': start_locations['Latitude'][start],
            'Product 1 Capacity': fuel1, 'Product 2 Capacity': fuel2})
        delivery_vehicles.to_json(appdata_path('delivery_vehicles.json'))
        self.route_vehicle.clear()
        self.route_vehicle.addItems(map(str, delivery_vehicles['Vehicle No.']))
        self.table4.model()._data = delivery_vehicles
        self.table4.model().layoutChanged.emit()

    # ---- Route stops / route vehicles ----
    def add_route_stop(self):
        global route_stops
        location = delivery_locations.iloc[self.stop.currentIndex()]
        fuel1 = self.fuel1_amt.text() or 0
        fuel2 = self.fuel2_amt.text() or 0
        window_start = self.window_start.time().toString()
        window_end = self.window_end.time().toString()
        route_stops = append_row(route_stops, {
            'Location ID': location['Location ID'], 'Location Name': location['Location Name'],
            'Location Address': location['Address'], 'Longitude': location['Longitude'],
            'Latitude': location['Latitude'], 'Product 1 Delivery Amount': fuel1,
            'Product 2 Delivery Amount': fuel2, 'Delivery Window Start': window_start,
            'Delivery Window End': window_end})
        route_stops.to_json(appdata_path('route_stops.json'))
        self.table5.model()._data = route_stops
        self.table5.model().layoutChanged.emit()

    def add_vehicle_route(self):
        global route_vehicles
        vehicle = delivery_vehicles.iloc[self.route_vehicle.currentIndex()]
        fuel1 = int(self.fuel1_start.text() or 0)
        fuel2 = int(self.fuel2_start.text() or 0)
        fuel1_capacity = int(vehicle['Product 1 Capacity'])
        fuel2_capacity = int(vehicle['Product 2 Capacity'])
        if fuel1 > fuel1_capacity:
            QMessageBox.warning(self, 'Error', 'Starting ' + PRODUCT_1_LABEL + ' amount greater than capacity')
            return
        if fuel2 > fuel2_capacity:
            QMessageBox.warning(self, 'Error', 'Starting ' + PRODUCT_2_LABEL + ' amount greater than capacity')
            return
        route_vehicles = append_row(route_vehicles, {
            'Vehicle ID': vehicle['Vehicle ID'], 'Vehicle No.': str(vehicle['Vehicle No.']),
            'Start Location ID': vehicle['Start Location ID'], 'Start Location': vehicle['Start Location'],
            'Start Address': vehicle['Start Address'], 'Start Longitude': vehicle['Start Longitude'],
            'Start Latitude': vehicle['Start Latitude'], 'Product 1 Capacity': fuel1_capacity,
            'Product 2 Capacity': fuel2_capacity, 'Starting Product 1 Amount': fuel1,
            'Starting Product 2 Amount': fuel2})
        route_vehicles.to_json(appdata_path('route_vehicles.json'))
        self.table6.model()._data = route_vehicles
        self.table6.model().layoutChanged.emit()

    # ---- Drivers ----
    def add_driver(self):
        global drivers_list
        first = self.driver_first.text()
        last = self.driver_last.text()
        contact = self.driver_contact.text()
        provider = self.driver_contact_provider.currentText()
        drivers_list = append_row(drivers_list, {
            'Driver Name': first + ' ' + last, 'Driver Cell Number': contact,
            'Cell Provider': provider})
        drivers_list.to_json(appdata_path('drivers_list.json'))
        self.table7.model()._data = drivers_list
        self.table7.model().layoutChanged.emit()

    # ---- Templates ----
    def save_route_template(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Save Template")
        layout = QVBoxLayout()
        template_name = QLineEdit()
        template_name.setPlaceholderText('Template Name')
        layout.addWidget(template_name)
        QBtn = QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        dlg.buttonBox = QDialogButtonBox(QBtn)
        dlg.buttonBox.accepted.connect(dlg.accept)
        dlg.buttonBox.rejected.connect(dlg.reject)
        layout.addWidget(dlg.buttonBox)
        dlg.setLayout(layout)
        if dlg.exec():
            name = str(template_name.text())
            target = appdata_path('route_templates', name)
            if os.path.exists(target):
                dlg2 = QMessageBox(self)
                dlg2.setWindowTitle("Template Name Already in Use")
                dlg2.setInformativeText("This template name is already being used. Overwrite?")
                dlg2.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if dlg2.exec() == QMessageBox.StandardButton.Yes:
                    route_stops.to_json(os.path.join(target, 'route_stops.json'))
                    route_vehicles.to_json(os.path.join(target, 'route_vehicles.json'))
            else:
                os.mkdir(target)
                route_stops.to_json(os.path.join(target, 'route_stops.json'))
                route_vehicles.to_json(os.path.join(target, 'route_vehicles.json'))
                self.template_select.addItem(name)

    def load_route_template(self):
        global route_stops, route_vehicles
        if self.template_select.isHidden():
            self.template_select.show()
            self.delete_template.show()
            self.load_template.setText('Load Selected Template')
        else:
            template = str(self.template_select.currentText())
            base = appdata_path('route_templates', template)
            route_stops = pd.read_json(os.path.join(base, 'route_stops.json'))
            self.table5.model()._data = route_stops
            self.table5.model().layoutChanged.emit()
            route_vehicles = pd.read_json(os.path.join(base, 'route_vehicles.json'))
            self.table6.model()._data = route_vehicles
            self.table6.model().layoutChanged.emit()
            self.template_select.hide()
            self.load_template.setText('Open Templates')
            self.delete_template.hide()

    def delete_route_template(self):
        template = str(self.template_select.currentText())
        base = appdata_path('route_templates', template)
        os.unlink(os.path.join(base, 'route_stops.json'))
        os.unlink(os.path.join(base, 'route_vehicles.json'))
        os.rmdir(base)
        self.template_select.hide()
        self.load_template.setText('Open Templates')
        self.delete_template.hide()
        self.template_select.removeItem(self.template_select.currentIndex())

    # ---- Dispatch (optional) ----
    def send_routes(self):
        host = os.environ.get('ROUTING_SMTP_HOST', 'smtp.gmail.com')
        port = int(os.environ.get('ROUTING_SMTP_PORT', '587'))
        user = os.environ.get('ROUTING_SMTP_USER')
        password = os.environ.get('ROUTING_SMTP_PASS')
        if not user or not password:
            QMessageBox.warning(
                self, 'SMTP Not Configured',
                'Set ROUTING_SMTP_USER and ROUTING_SMTP_PASS environment variables '
                'to enable sending routes to drivers.')
            return
        server = smtplib.SMTP(host, port)
        server.starttls()
        server.login(user, password)
        for route in self.driver_list:
            number = drivers_list['Driver Cell Number'][route['ComboBox'].currentIndex()]
            provider = drivers_list['Cell Provider'][route['ComboBox'].currentIndex()]
            truck = route['Truck']
            coords = list(route['Coordinates'])
            origin = coords[0]
            waypoints = coords[1:]
            number_address = str(number) + MMS_GATEWAYS[provider]
            link = ('https://www.google.com/maps/dir/?api=1&origin=' + urllib.parse.quote(str(origin))
                    + '&destination=' + urllib.parse.quote(str(origin))
                    + '&waypoints=' + urllib.parse.quote('|'.join(map(str, waypoints)))
                    + '&travelmode=driving&dir_action=navigate')
            msg = MIMEText(link)
            msg['Subject'] = 'Route for ' + truck + ':'
            server.sendmail(user, number_address, msg.as_string())
        server.quit()

    # ---- Run the optimizer ----
    def run_algorithm(self):
        if not self.max_shift.text() or not self.fuel_rate.text():
            QMessageBox.warning(self, 'Error', 'Enter a service rate and a max shift length.')
            return
        if api_key == '':
            QMessageBox.warning(self, 'Error', 'Enter and save your LocationIQ API key first.')
            return
        clearLayout(self.output_layout)

        spin_gif = QMovie("routing.gif")
        busy = QLabel()
        busy.setMovie(spin_gif)
        spin_gif.start()
        movie_tag = QLabel('Planning Routes...')
        movie_tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        movie_tag.setProperty('title', True)
        self.output_layout.addWidget(busy)
        self.output_layout.addWidget(movie_tag)
        self.output_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        hour, minute, sec = map(int, self.day_start.time().toString().split(':'))
        params = {
            'max_time': int(float(self.max_shift.text()) * 3600),
            'day_start': 60 * (hour * 60 + minute),
            'service_rate': float(self.fuel_rate.text()),
        }
        self._worker = RouteWorker(params)
        self._worker.finished_ok.connect(self.on_routes_ready)
        self._worker.failed.connect(self.on_routes_failed)
        self._worker.start()

    def on_routes_failed(self, message):
        clearLayout(self.output_layout)
        QMessageBox.critical(self, 'Routing Error', message)

    def on_routes_ready(self, solution):
        clearLayout(self.output_layout)
        if isinstance(solution, str):
            self.output_layout.addWidget(QLabel(solution))
            return

        self.driver_list = []
        route_widget = QWidget()
        route_widget.setMinimumSize(QSize(600, 400))
        routes_layout = QVBoxLayout(route_widget)
        for truck_id in solution[0]:
            if len(solution[0][truck_id]['dataframe']) > 2:
                truck_label = QLabel(truck_id)
                truck_label.setProperty('title', True)
                self.route_driver = QComboBox()
                self.route_driver.addItems(drivers_list['Driver Name'])
                self.driver_list.append({
                    'ComboBox': self.route_driver, 'Truck': truck_id,
                    'Coordinates': solution[0][truck_id]['dataframe']['Location Address']})
                route_table_row = QHBoxLayout()
                route_table_row.addWidget(truck_label)
                route_table_row.addStretch()
                route_table_row.addWidget(QLabel('Select Driver:'))
                route_table_row.addWidget(self.route_driver)
                routes_layout.addLayout(route_table_row)
                route_table = QTableView()
                route_table.setModel(PandasModel(solution[0][truck_id]['dataframe'], [], ''))
                route_table_layout = QVBoxLayout()
                route_table_layout.addWidget(route_table)
                routes_layout.addLayout(route_table_layout)
                route_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
                route_table.horizontalHeader().setMinimumHeight(40)
                totals = QLabel('Total Delivered: ' + str(solution[0][truck_id]['Total Delivered'])
                                + ' ' + UNIT_LABEL + '   Total Time: ' + str(solution[0][truck_id]['Total Time'])
                                + ' min   Total Distance: ' + str(solution[0][truck_id]['Total Distance']) + ' miles')
                routes_layout.addWidget(totals)
            else:
                truck_label = QLabel(truck_id + ' - No Route')
                truck_label.setProperty('title', True)
                routes_layout.addWidget(truck_label)

        routes_scroll = QScrollArea()
        routes_scroll.setWidget(route_widget)
        routes_scroll.setWidgetResizable(True)
        routes_scroll.setMinimumSize(QSize(600, 400))
        self.output_layout.addWidget(routes_scroll)

        self.solution = QLabel(solution[1])
        self.send_routes_button = QPushButton('Send Routes to Drivers')
        self.send_routes_button.clicked.connect(self.send_routes)
        final_row = QHBoxLayout()
        final_row.addWidget(self.solution)
        final_row.addStretch()
        final_row.addWidget(self.send_routes_button)

        self.view = QtWebEngineWidgets.QWebEngineView()
        self.view.setMinimumSize(QSize(600, 600))
        self.view.setProperty('mapbox', True)
        data = io.BytesIO()
        solution[2].save(data, close_file=False)
        self.view.setHtml(data.getvalue().decode())

        self.output_layout.addLayout(final_row)
        self.output_layout.addWidget(self.view)

    def toggle_theme(self):
        sshFile = "dark_style.txt" if self.theme.isChecked() else "style.txt"
        if os.path.exists(sshFile):
            with open(sshFile, "r") as fh:
                self.setStyleSheet(fh.read())


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()




