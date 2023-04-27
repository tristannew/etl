The Garden step is where most of the work falls in. This is where the data manager needs to carefully look at the data, filter outliers, harmonize labels (e.g. country names), improve the dataset metadata, etc.

Garden steps typically depend on meadow steps. For instance, the `garden` step `data://garden/un/2022-07-11/un_wpp`, which generates the dataset _World Population Prospects (UN, 2022)_, depends on this same process but in `meadow`: `data://meadow/un/2022-07-11/un_wpp`. That is, the `meadow` step has done some pre-liminary (and minor) work on re-shaping the UN WPP dataset and now is the turn of the `garden` step to apply the major processing steps to have a curated UN WPP dataset by Our World in Data.

A typical flow up to the Garden step could look like:


```mermaid
flowchart LR

    upstream1((____)):::node -.->|copy| snapshot1((____)):::node
    snapshot1((____)):::node -->|format| meadow1((____)):::node
    meadow1((____)):::node -->|harmonize| garden1((____)):::node

    subgraph id0 [Upstream]
    upstream1
    end

    subgraph id1 [Snapshot]
    snapshot1
    end

    subgraph id2 [Meadow]
    meadow1
    end

    subgraph id3 [Garden]
    garden1
    end

    subgraph id [ETL]
    id1
    id2
    id3
    end

    classDef node fill:#002147,color:#002147
    classDef node_ss fill:#002147,color:#fff
```

However, grapher steps could also depend on other grapher steps. This is often the case for datasets containing _long-run variables_, where different `garden` datasets are combined.


!!! info "Long-run variables"

    A long-run variable is a variable that has datapoints spanning over a broad period of time. For instance, we have a [population variable](https://ourworldindata.org/population-sources) that combines data from the UN and other sources that goes back to 10,000 BCE. In particular, it uses data from the UN, Gapminder and HYDE.

    This is how the dependency graph our population variable looks like:

    ```yaml
      data://garden/demography/2023-03-31/population:
      - data://garden/hyde/2017/baseline
      - data://garden/gapminder/2023-03-31/population
      - data://garden/un/2022-07-11/un_wpp
      - data://open_numbers/open_numbers/latest/gapminder__systema_globalis
    ```

!!! bug "TODO: Add an example of code"
## Harmonizing labels

In order to understand data within a single dataset, we want to know what is meant by the data.

For example, a `country` column containing the value `Korea` could be referring to South Korea, North Korea, or historical unified Korea as it existed a century ago, depending on the context and the intent of the data provider.

Harmonization is the editorial process by which we modify the indexing columns for a dataset to ensure that the data is consistent and unambiguous.

### What does Our World In Data harmonize?

Today, Our World In Data makes a best-effort to harmonize countries and regions. We strive to do this in a way that is consistent with the [ISO 3166-1 standard](https://en.wikipedia.org/wiki/ISO_3166-1), however we use custom editorial labels for countries and regions that are often shorter than those in the standard, in order to make data visualisations richer and more understandable.

Since we also present long-run datasets over multiple centuries, a time period in which national borders have changed, split and merged, we also make a best-effort attempt to harmonize the names of historical countries and regions that no longer exist and are not present in the ISO standard.

### How do we perform harmonization?

There are two methods that we use, both of which are semi-automated and involve some human judgement by our data managers.

#### Command-line harmonization

!!! success "Recommended method"

The [etl](https://github.com/owid/etl) codebase contains an interactive `harmonize` command-line tool which can be used to harmonize a CSV file that contains a column with country names.

```
$ poetry run harmonize --help
Usage: harmonize [OPTIONS] DATA_FILE COLUMN OUTPUT_FILE [INSTITUTION]

  Given a DATA_FILE in feather or CSV format, and the name of the COLUMN
  representing country or region names, interactively generate the JSON
  mapping OUTPUT_FILE from the given names to OWID's canonical names.
  Optionally, can use INSTITUTION to append "(institution)" to countries.

  When a name is ambiguous, you can use:

  n: to ignore and skip to the next one

  s: to suggest matches; you can also enter a manual match here

  q: to quit and save the partially complete mapping

  If a mapping file already exists, it will resume where the mapping file
  left off.

Options:
  --help  Show this message and exit.
```

As an example, start the harmonization interactive session for table `undp_hdr` from dataset `meadow/un/2022-11-29/undp_hdr`, which has `country` column with the raw country names:


```bash
$ poetry run harmonize data/meadow/un/2022-11-29/undp_hdr/undp_hdr.feather country mapping.json
206 countries/regions to harmonize
  └ 189 automatically matched
  └ 17 ambiguous countries/regions

Beginning interactive harmonization...

[1/17] Arab States
(n) next, (s) suggest, (q) quit
(Cmd)
```

The output mapping is saved in `mapping.json`.

#### Using the Grapher admin

!!! warning "This method is not preferred. Instead, consider using the `harmonize` command tool."

The [owid-grapher](https://github.com/owid/owid-grapher) codebase contains a interactive country harmonization tool that can be accessed at [http://localhost:3030/admin/standardize](http://localhost:3030/admin/standardize) when running the dev server.

To use the tool, you upload a CSV file containing a column called `Country`, and indicate the encoding of country names.

??? Tip "For staff"

    The interactive harmonization tool for staff is available at [https://owid.cloud/admin/standardize](https://owid.cloud/admin/standardize).


## Metadata
In our [data model](../design/common-format.md), datasets contain tables of data, and those tables contain variables. Each of these levels supports metadata.

!!! warning "This is still being written."

    Our metadata formats are still in flux, and are likely to change substantially over the coming year.

    Most up-to-date documentation may be found [on Notion :octicons-arrow-right-24:](https://www.notion.so/owid/Metadata-guidelines-29ca6e19b6f1409ea6826a88dbb18bcc)




!!! info "For more up to date details, see the the classes `DatasetMeta`, `TableMeta` and `VariableMeta` in the [`owid.catalog.meta`](https://github.com/owid/owid-catalog-py/blob/master/owid/catalog/meta.py) module."