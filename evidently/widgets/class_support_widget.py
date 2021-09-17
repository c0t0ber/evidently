#!/usr/bin/env python
# coding: utf-8

import json
import pandas as pd

import plotly.graph_objs as go

from evidently.analyzers.classification_performance_analyzer import ClassificationPerformanceAnalyzer
from evidently.model.widget import BaseWidgetInfo, AlertStats
from evidently.widgets.widget import Widget, RED


class ClassSupportWidget(Widget):
    def __init__(self, title: str, dataset: str='reference'):
        super().__init__(title)
        self.dataset = dataset #reference or current

    def analyzers(self):
        return [ClassificationPerformanceAnalyzer]

    def calculate(self,
                  reference_data: pd.DataFrame,
                  current_data: pd.DataFrame,
                  column_mapping,
                  analyzers_results):

        results = analyzers_results[ClassificationPerformanceAnalyzer]

        if results['utility_columns']['target'] is not None and results['utility_columns']['prediction'] is not None:
            if self.dataset in results['metrics'].keys():

                #plot support bar
                metrics_frame = pd.DataFrame(results['metrics'][self.dataset]['metrics_matrix'])

                fig = go.Figure()

                fig.add_trace(go.Bar(x=results['target_names'] if results['target_names'] else metrics_frame.columns.tolist()[:-3],
                    y=metrics_frame.iloc[-1:,:-3].values[0], marker_color=RED, name='Support'))

                fig.update_layout(
                    xaxis_title = "Class",
                    yaxis_title = "Number of Objects",
                )

                support_bar_json = json.loads(fig.to_json())

                self.wi = BaseWidgetInfo(
                    title=self.title,
                    type="big_graph",
                    details="",
                    alertStats=AlertStats(),
                    alerts=[],
                    alertsPosition="row",
                    insights=[],
                    size=1 if current_data is not None else 2,
                    params={
                        "data": support_bar_json['data'],
                        "layout": support_bar_json['layout']
                    },
                    additionalGraphs=[],
                )
            else:
                self.wi = None
        else:
            self.wi = None
