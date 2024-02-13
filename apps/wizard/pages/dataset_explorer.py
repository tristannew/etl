"""Explore a dataset from ETL.

- [ ] See its dependencies
- [ ] Preview its metadata
"""
from typing import Any, Dict, List, cast

import pandas as pd
import streamlit as st
from owid import catalog
from st_pages import add_indentation
from streamlit_agraph import Config, ConfigBuilder, Edge, Node, agraph

from etl.paths import DATA_DIR
from etl.steps import extract_step_attributes, filter_to_subgraph, load_dag

# CONFIG
st.set_page_config(
    page_title="Dataset Explorer",
    layout="wide",
    page_icon="🕵️",
    initial_sidebar_state="collapsed",
)
st.title("🕵️ Dataset Explorer")
add_indentation()

COLORS = {
    "snapshot": "#FC9090",
    "walden": "#FC9090",
    "meadow": "#F5DB49",
    "garden": "#87E752",
    "grapher": "#67AAE1",
}
COLOR_OTHER = "#B6B6B6"
COLOR_MAIN = "#81429A"


def activate():
    st.session_state["show"] = True


def display_metadata(metadata):
    if not isinstance(metadata, dict):
        metadata = metadata.to_dict()
    ds = pd.Series(metadata)
    ds.name = "value"
    st.table(data=ds)


def generate_graph(
    dag: Dict[str, Any],
    uri_main: str,
    collapse_snapshot: bool = True,
    collapse_others: bool = True,
    collapse_meadow: bool = True,
) -> Any:
    def _friendly_label(attributes: Dict[str, str], length_limit: int = 32) -> str:
        label_1 = f"{attributes['namespace']}/{attributes['name']}"
        if len(label_1) > length_limit:
            label_1 = label_1[:length_limit] + "..."
        label = f"{label_1}\n{attributes['version']}"
        return label

    def _friendly_title(attributes: Dict[str, str], children: List[str]) -> str:
        deps = "\n- ".join(children)
        title = f"""{attributes['identifier'].upper()}
        version {attributes['version']} ({attributes['kind']})
        """
        title = attributes["step"].upper()
        if deps:
            title = title + "\n\ndependencies:\n- " + deps
        return title

    def _collapse_node(attributes: Dict[str, str]) -> bool:
        if collapse_snapshot and (attributes["channel"] in ["snapshot", "walden"]):
            return True
        if collapse_meadow and (attributes["channel"] in ["meadow"]):
            return True
        if collapse_others and (attributes["channel"] not in ["snapshot", "walden", "meadow", "garden", "grapher"]):
            return True
        return False

    # Create edges
    edges = []
    nodes = []
    for parent, children in dag.items():
        attributes = extract_step_attributes(parent)

        # Main node
        if parent == uri_main:
            kwargs = {
                "color": COLORS.get(attributes["channel"], COLOR_OTHER),
                "label": f"{attributes['namespace'].upper()}/{attributes['name'].upper()}\n{attributes['version']}",
                "title": _friendly_title(attributes, children),
                "font": {
                    "size": 40,
                    "face": "courier",
                    "align": "left",
                },
                "mass": 2,
            }
        # Oth nod (dependencies)
        else:
            # Nodes that will not show label within them (user chose to hide them)
            kwargs = {
                "color": COLORS.get(attributes["channel"], COLOR_OTHER),
                "mass": 1,
                "opacity": 0.9,
            }
            if _collapse_node(attributes):
                kwargs = {
                    **kwargs,
                    "title": _friendly_title(attributes, children),
                    "mass": 1,
                    "opacity": 0.9,
                }
            # Nodes that will show label within them
            else:
                kwargs = {
                    **kwargs,
                    "label": _friendly_label(attributes),
                    "title": _friendly_title(attributes, children),
                    "font": {
                        "size": 20,
                        "face": "courier",
                        "align": "left",
                    },
                }

        # Change if step is private
        if attributes["kind"] == "private":
            kwargs["label"] = "🔒 " + kwargs.get("label", "")

        node = Node(
            id=parent,
            borderWidthSelected=5,
            margin=10,
            shape="box",
            borderWidth=5,
            **kwargs,
        )
        nodes.append(node)
        for child in children:
            edge = Edge(
                source=child,
                target=parent,
                width=5,
            )
            edges.append(edge)

    config_builder = ConfigBuilder(nodes)
    config = config_builder.build()

    # node_config = {
    #     "labelProperty": "label",
    #     "renderLabel": "true",
    # }

    # config.layout["hierarchical"]["enabled"] = True
    config = Config(
        width=2000,
        height=1000,
        nodeHighlightBehavior=True,
        # highlightColor="#F7A7A6",
        # collapsible=True,
        # node=node_config,
        directed=True,
        physics=False,
        minVelocity=20,
        maxVelocity=1000,
        # nodeSpacing=10000,
        stabilization=False,
        fit=False,
        hierarchical=True,
        # nodeSpacing=200,
        # **kwargs
    )

    # config.physics["barnesHut"] = {"springConstant": 0, "avoidOverlap": 0.1}

    return agraph(nodes=nodes, edges=edges, config=config)


with st.form("form"):
    dag = load_dag()
    options = sorted(list(dag.keys()))
    option = st.selectbox("Select a dataset", options)

    # Add toggles
    help_template = "Show nodes of type '{channel}' as dots, without text. This can make the visualisation cleaner.The step URI will still be visible on hover."
    collapse_others = st.toggle("Collapse others", value=True, help=help_template.format(channel="other"))
    collapse_snapshot = st.toggle(
        "Collapse snapshot/walden", value=True, help=help_template.format(channel="snapshot/walden")
    )
    collapse_meadow = st.toggle("Collapse meadow", value=False, help=help_template.format(channel="meadow"))
    downstream = st.toggle("Show downstream", value=False, help="Show nodes that depend on the selected node.")
    # Form submit button
    st.form_submit_button("Explore", on_click=activate)


if st.session_state.get("show"):
    # Show graph
    def _show_tab_graph(tab, option, dag):
        with tab:
            with st.spinner(f"Generating graph {option}..."):
                dag = filter_to_subgraph(dag, downstream=downstream, includes=[cast(str, option)])

                with st.expander("Show raw DAG"):
                    st.write(dag)
                if option is None:
                    option = options[0]
                _ = generate_graph(dag, option, collapse_snapshot, collapse_others, collapse_meadow)

    # Get dataset attributes, check we want to display it
    attributes = extract_step_attributes(cast(str, option))
    show_d_details = attributes["channel"] in ["garden", "grapher"]

    if show_d_details:
        tab_graph, tab_details = st.tabs(["Dependency graph", "Dataset details"])

        # Show graph
        _show_tab_graph(tab_graph, option, dag)

        # Show dataset details
        ## Load dataset
        attributes = extract_step_attributes(cast(str, option))
        dataset_path = (
            DATA_DIR / f"{attributes['channel']}/{attributes['namespace']}/{attributes['version']}/{attributes['name']}"
        )
        dataset = None
        try:
            dataset = catalog.Dataset(dataset_path)
        except Exception:
            st.warning(f"Dataset not found. You may want to run `etl {attributes['step']}` first")
        else:
            # Display metadata
            display_metadata(dataset.metadata)

        # Tables and indicators
        if dataset is not None:
            tnames = [f"`{tname}`" for tname in dataset.table_names]
            st.info(f"This dataset has {len(tnames)} tables: {', '.join(tnames)}")
            for tname in dataset.table_names:
                st.write(f"### Table `{tname}`")
                tb = dataset[tname]
                # Metadata for each indicator
                tabs = st.tabs([f"`{colname}`" for colname in tb.columns])
                for tab, colname in zip(tabs, tb.columns):
                    with tab:
                        metadata = tb[colname].metadata.to_dict()
                        origins = metadata.pop("origins", None)
                        presentation = metadata.pop("presentation", None)
                        display = metadata.pop("display", None)

                        # Display metadata
                        display_metadata(metadata)

                        if display:
                            with st.expander("display.*"):
                                display_metadata(display)
                        if presentation:
                            with st.expander("presentation.*"):
                                display_metadata(presentation)
                        if origins:
                            with st.expander("origins.*"):
                                for origin in origins:
                                    display_metadata(origin)

    else:
        tab_graph = st.tabs(["Dependency graph"])
        _show_tab_graph(tab_graph[0], option, dag)
    # Set back to False
    # st.session_state["show_gpt"] = False