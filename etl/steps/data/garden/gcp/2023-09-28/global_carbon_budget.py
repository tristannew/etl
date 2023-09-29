"""This step creates the Global Carbon Budget (GCB) dataset, by the Global Carbon Project (GCP).

It harmonizes and further processes meadow data, and uses the following auxiliary datasets:
- GGDC's Maddison dataset on GDP, used to calculate emissions per GDP.
- Primary Energy Consumption (mix of sources from the 'energy' namespace) to calculate emissions per unit energy.
- Population (mix of sources), to calculate emissions per capita.
- Regions (mix of sources), to generate aggregates for different continents.
- WorldBank's Income groups, to generate aggregates for different income groups.

"""
import numpy as np
import owid.catalog.processing as pr
from owid.catalog import Dataset, Table
from owid.datautils import dataframes
from structlog import get_logger

from etl.data_helpers import geo
from etl.helpers import PathFinder, create_dataset

log = get_logger()

# Get paths and naming conventions for current step.
paths = PathFinder(__file__)

# Expected outliers in consumption-based emissions (with negative emissions in the original data, that will be removed).
OUTLIERS_IN_CONSUMPTION_DF = [
    ("Panama", 2003),
    ("Panama", 2004),
    ("Panama", 2005),
    ("Panama", 2006),
    ("Panama", 2011),
    ("Panama", 2012),
    ("Panama", 2013),
    ("Venezuela", 2018),
]

# Label used for international transport (emissions from oil in bunker fuels), included as a country in the
# fossil CO2 emissions dataset.
INTERNATIONAL_TRANSPORT_LABEL = "International Transport"

# Regions and income groups to create by aggregating contributions from member countries.
# In the following dictionary, if nothing is stated, the region is supposed to be a default continent/income group.
# Otherwise, the dictionary can have "regions_included", "regions_excluded", "countries_included", and
# "countries_excluded". The aggregates will be calculated on the resulting countries.
REGIONS = {
    # Default continents.
    "Africa": {},
    "Asia": {},
    "Europe": {},
    "European Union (27)": {},
    "North America": {},
    "Oceania": {},
    "South America": {},
    # Income groups.
    "Low-income countries": {},
    "Upper-middle-income countries": {},
    "Lower-middle-income countries": {},
    "High-income countries": {},
    # Additional composite regions.
    "Asia (excl. China and India)": {
        "additional_regions": ["Asia"],
        "excluded_members": ["China", "India"],
    },
    "Europe (excl. EU-27)": {"additional_regions": ["Europe"], "excluded_regions": ["European Union (27)"]},
    "Europe (excl. EU-28)": {
        "additional_regions": ["Europe"],
        "excluded_regions": ["European Union (27)"],
        "excluded_members": ["United Kingdom"],
    },
    "European Union (28)": {
        "additional_regions": ["European Union (27)"],
        "additional_members": ["United Kingdom"],
    },
    "North America (excl. USA)": {
        "additional_regions": ["North America"],
        "excluded_members": ["United States"],
    },
}

# Columns to use from GCB fossil CO2 emissions data and how to rename them.
CO2_COLUMNS = {
    "country": "country",
    "year": "year",
    "cement": "emissions_from_cement",
    "coal": "emissions_from_coal",
    "flaring": "emissions_from_flaring",
    "gas": "emissions_from_gas",
    "oil": "emissions_from_oil",
    "other": "emissions_from_other_industry",
    "total": "emissions_total",
}

# List all sources of emissions considered.
EMISSION_SOURCES = [column for column in CO2_COLUMNS.values() if column not in ["country", "year"]]

# Columns to use from primary energy consumption data and how to rename them.
PRIMARY_ENERGY_COLUMNS = {
    "country": "country",
    "year": "year",
    "primary_energy_consumption__twh": "primary_energy_consumption",
}

# Columns to use from primary energy consumption data and how to rename them.
HISTORICAL_EMISSIONS_COLUMNS = {
    "country": "country",
    "year": "year",
    # Global fossil emissions are used only for sanity checks.
    "global_fossil_emissions": "global_fossil_emissions",
    "global_land_use_change_emissions": "global_emissions_from_land_use_change",
}

# Columns to use from consumption-based emissions data and how to rename them.
CONSUMPTION_EMISSIONS_COLUMNS = {
    "country": "country",
    "year": "year",
    "consumption_emissions": "consumption_emissions",
}

# Conversion from terawatt-hours to kilowatt-hours.
TWH_TO_KWH = 1e9

# Conversion factor to change from billion tonnes of carbon to tonnes of CO2.
BILLION_TONNES_OF_CARBON_TO_TONNES_OF_CO2 = 3.664 * 1e9

# Conversion factor to change from million tonnes of carbon to tonnes of CO2.
MILLION_TONNES_OF_CARBON_TO_TONNES_OF_CO2 = 3.664 * 1e6

# Conversion from million tonnes of CO2 to tonnes of CO2.
MILLION_TONNES_OF_CO2_TO_TONNES_OF_CO2 = 1e6

# Conversion from tonnes of CO2 to kg of CO2 (used for emissions per GDP and per unit energy).
TONNES_OF_CO2_TO_KG_OF_CO2 = 1000

# In order to remove uninformative columns, keep only rows where at least one of the following columns has data.
# All other columns are either derived variables, or global variables, or auxiliary variables from other datasets.
COLUMNS_THAT_MUST_HAVE_DATA = [
    "emissions_from_cement",
    "emissions_from_coal",
    "emissions_from_flaring",
    "emissions_from_gas",
    "emissions_from_oil",
    "emissions_from_other_industry",
    "emissions_total",
    "consumption_emissions",
    "emissions_from_land_use_change",
    # 'land_use_change_quality_flag',
]


def sanity_checks_on_input_data(
    tb_production: Table, tb_consumption: Table, tb_historical: Table, tb_co2: Table
) -> None:
    """Run sanity checks on input data files.

    These checks should be used prior to country harmonization, but after basic processing of the tables.

    Parameters
    ----------
    tb_production : Table
        Production-based emissions from GCP's official national emissions dataset (excel file).
    tb_consumption : Table
        Consumption-based emissions from GCP's official national emissions dataset (excel file).
    tb_historical : Table
        Historical emissions from GCP's official global emissions dataset (excel file).
    tb_co2 : Table
        Production-based emissions from GCP's Fossil CO2 emissions dataset (csv file).

    """
    tb_production = tb_production.copy()
    tb_consumption = tb_consumption.copy()
    tb_historical = tb_historical.copy()
    tb_co2 = tb_co2.copy()

    # In the original data, Bunkers was included in the national data file, as another country.
    # But I suppose it should be considered as another kind of global emission.
    # In fact, bunker emissions should coincide for production and consumption emissions.
    global_bunkers_emissions = (
        tb_production[tb_production["country"] == "Bunkers"][["year", "production_emissions"]]
        .reset_index(drop=True)
        .rename(columns={"production_emissions": "global_bunker_emissions"}, errors="raise")
    )

    # Check that we get exactly the same array of bunker emissions from the consumption emissions table
    # (on years where there is data for bunker emissions in both datasets).
    comparison = pr.merge(
        global_bunkers_emissions,
        tb_consumption[tb_consumption["country"] == "Bunkers"][["year", "consumption_emissions"]]
        .reset_index(drop=True)
        .rename(columns={"consumption_emissions": "global_bunker_emissions"}, errors="raise"),
        how="inner",
        on="year",
        suffixes=("", "_check"),
    )

    error = "Bunker emissions were expected to coincide in production and consumption emissions tables."
    assert (comparison["global_bunker_emissions"] == comparison["global_bunker_emissions_check"]).all(), error

    # Check that all production-based emissions are positive.
    error = "There are negative emissions in tb_production (from the additional variables dataset)."
    assert (tb_production.drop(columns=["country", "year"]).fillna(0) >= 0).all().all(), error

    # Check that all production-based emissions from the fossil CO2 dataset are positive.
    error = "There are negative emissions in tb_co2 (from the fossil CO2 dataset)."
    assert (tb_co2.drop(columns=["country", "year"]).fillna(0) >= 0).all().all(), error

    # Check that all consumption-based emissions are positive.
    error = "There are negative emissions in tb_consumption (from the national emissions dataset)."
    assert (tb_consumption.drop(columns=["country", "year"]).fillna(0) >= 0).all().all(), error

    # Check that, for the World, production emissions coincides with consumption emissions (on common years).
    error = "Production and consumption emissions for the world were expected to be identical."
    comparison = pr.merge(
        tb_production[tb_production["country"] == "World"].reset_index(drop=True),
        tb_consumption[tb_consumption["country"] == "World"].reset_index(drop=True),
        how="inner",
        on="year",
    )
    assert (comparison["production_emissions"] == comparison["consumption_emissions"]).all(), error

    # Check that production emissions for the World coincide with global (historical) emissions (on common years).
    comparison = pr.merge(
        tb_production[tb_production["country"] == "World"][["year", "production_emissions"]].reset_index(drop=True),
        tb_historical[["year", "global_fossil_emissions"]],
        how="inner",
        on="year",
    )
    error = "Production emissions for the world were expected to coincide with global fossil emissions."
    assert (
        abs(comparison["production_emissions"] - comparison["global_fossil_emissions"])
        / (comparison["global_fossil_emissions"])
        < 0.001
    ).all(), error

    # Check that emissions in tb_production (emissions from the national excel file) coincide with emissions in tb_co2
    # (from the Fossil CO2 emissions csv file).
    # Given that country names have not yet been harmonized, rename the only countries that are present in both datasets.
    comparison = pr.merge(
        tb_co2[["country", "year", "emissions_total"]],
        tb_production.replace({"Bunkers": "International Transport", "World": "Global"}),
        on=["country", "year"],
        how="inner",
    ).dropna(subset=["emissions_total", "production_emissions"], how="any")
    # Since we included the emissions from the Kuwaiti oil fires in Kuwait (and they are not included in tb_production),
    # omit that row in the comparison.
    comparison = comparison.drop(
        comparison[(comparison["country"] == "Kuwait") & (comparison["year"] == 1991)].index
    ).reset_index(drop=True)

    error = "Production emissions from national file were expected to coincide with the Fossil CO2 emissions dataset."
    assert (
        (
            100
            * abs(comparison["production_emissions"] - comparison["emissions_total"])
            / (comparison["emissions_total"])
        ).fillna(0)
        < 0.1
    ).all(), error


def sanity_checks_on_output_data(tb_combined: Table) -> None:
    """Run sanity checks on output data.

    These checks should be run on the very final output table (with an index) prior to storing it as a table.

    Parameters
    ----------
    combined_df : Table
        Combination of all input tables, after processing, harmonization, and addition of variables.

    """
    tb_combined = tb_combined.reset_index()
    error = "All variables (except traded emissions, growth, and land-use change) should be >= 0 or nan."
    positive_variables = [
        col
        for col in tb_combined.columns
        if col != "country"
        if "traded" not in col
        if "growth" not in col
        if "land_use" not in col
    ]
    assert (tb_combined[positive_variables].fillna(0) >= 0).all().all(), error

    error = "Production emissions as a share of global emissions should be 100% for 'World' (within 2% error)."
    assert tb_combined[
        (tb_combined["country"] == "World") & (abs(tb_combined["emissions_total_as_share_of_global"] - 100) > 2)
    ].empty, error

    error = "Consumption emissions as a share of global emissions should be 100% for 'World' (within 2% error)."
    assert tb_combined[
        (tb_combined["country"] == "World") & (abs(tb_combined["consumption_emissions_as_share_of_global"] - 100) > 2)
    ].empty, error

    error = "Population as a share of global population should be 100% for 'World'."
    assert tb_combined[
        (tb_combined["country"] == "World") & (tb_combined["population_as_share_of_global"].fillna(100) != 100)
    ].empty, error

    error = "All share of global emissions should be smaller than 100% (within 2% error)."
    share_variables = [col for col in tb_combined.columns if "share" in col]
    assert (tb_combined[share_variables].fillna(0) <= 102).all().all(), error

    # Check that cumulative variables are monotonically increasing.
    # Firstly, list columns of cumulative variables, but ignoring cumulative columns as a share of global
    # (since they are not necessarily monotonic) and land-use change (which can be negative).
    cumulative_cols = [
        col for col in tb_combined.columns if "cumulative" in col if "share" not in col if "land_use" not in col
    ]
    # Using ".is_monotonic_increasing" can fail when differences between consecutive numbers are very small.
    # Instead, sort data backwards in time, and check that consecutive values of cumulative variables always have
    # a percentage change that is smaller than, say, 0.1%.
    error = (
        "Cumulative variables (not given as a share of global) should be monotonically increasing (except when "
        "including land-use change emissions, which can be negative)."
    )
    assert (
        tb_combined.sort_values("year", ascending=False)
        .groupby("country")
        .agg({col: lambda x: ((x.pct_change().dropna() * 100) <= 0.1).all() for col in cumulative_cols})
        .all()
        .all()
    ), error

    error = (
        "Production emissions as a share of global production emissions for the World should always be 100% "
        "(or larger than 98%, given small discrepancies)."
    )
    # Consumption emissions as a share of global production emissions is allowed to be smaller than 100%.
    share_variables = [col for col in tb_combined.columns if "share" in col if "consumption" not in col]
    assert (tb_combined[tb_combined["country"] == "World"][share_variables].fillna(100) > 98).all().all(), error

    error = "Traded emissions for the World should be close to zero (within 2% error)."
    world_mask = tb_combined["country"] == "World"
    assert (
        abs(
            100
            * tb_combined[world_mask]["traded_emissions"].fillna(0)
            / tb_combined[world_mask]["emissions_total"].fillna(1)
        )
        < 2
    ).all(), error


def prepare_fossil_co2_emissions(tb_co2: Table) -> Table:
    """Prepare Fossil CO2 emissions data (basic processing)."""
    # Select and rename columns from fossil CO2 data.
    tb_co2 = tb_co2[list(CO2_COLUMNS)].rename(columns=CO2_COLUMNS, errors="raise")

    # Ensure all emissions are given in tonnes of CO2.
    tb_co2[EMISSION_SOURCES] *= MILLION_TONNES_OF_CO2_TO_TONNES_OF_CO2

    ####################################################################################################################
    # NOTE: For certain years, column "emissions_from_other_industry" is not informed for "World" but it is informed
    # for some countries (namely China and US).
    # This causes the cumulative emissions from other industry as share of global for those countries to become larger
    # than 100%.
    # This temporary solution fixes the issue: We aggregate the data for China and US on those years when the world's
    # data is missing (without touching other years or other columns).
    # Firstly, list of years for which the world has no data for emissions_from_other_industry.
    world_missing_years = (
        tb_co2[(tb_co2["country"] == "Global") & (tb_co2["emissions_from_other_industry"].isnull())]["year"]
        .unique()
        .tolist()  # type: ignore
    )
    # Data that needs to be aggregated.
    data_missing_in_world = tb_co2[
        tb_co2["year"].isin(world_missing_years) & (tb_co2["emissions_from_other_industry"].notnull())
    ]
    # Check that there is indeed data to be aggregated (that is missing for the World).
    error = (
        "Expected emissions_from_other_industry to be null for the world but not null for certain countries "
        "(which was an issue in the original fossil CO2 data). The issue may be fixed and the code can be simplified."
    )
    assert len(data_missing_in_world) > 0, error
    # Create a table of aggregate data for the World, on those years when it's missing.
    aggregated_missing_data = (
        data_missing_in_world.groupby("year")
        .agg({"emissions_from_other_industry": "sum"})
        .reset_index()
        .assign(**{"country": "Global"})
    )
    # Combine the new table of aggregate data with the main table.
    tb_co2 = dataframes.combine_two_overlapping_dataframes(
        df1=tb_co2, df2=aggregated_missing_data, index_columns=["country", "year"], keep_column_order=True
    )
    # NOTE: The previous function currently does not properly propagate metadata, but keeps only the sources of the
    # first table. But given that both tables combined have the same source, we don't need to manually change it.
    ####################################################################################################################

    # We add the emissions from "Kuwaiti Oil Fires" (which is also included as a separate country) as part of the
    # emissions of Kuwait. This ensures that they will be included in region aggregates.
    error = "'Kuwaiti Oil Fires' was expected to only have not-null data for 1991."
    assert tb_co2[
        (tb_co2["country"] == "Kuwaiti Oil Fires")
        & (tb_co2["emissions_total"].notnull())
        & (tb_co2["emissions_total"] != 0)
    ]["year"].tolist() == [1991], error

    tb_co2.loc[(tb_co2["country"] == "Kuwait") & (tb_co2["year"] == 1991), EMISSION_SOURCES] = (
        tb_co2[(tb_co2["country"] == "Kuwaiti Oil Fires") & (tb_co2["year"] == 1991)][EMISSION_SOURCES].values
        + tb_co2[(tb_co2["country"] == "Kuwait") & (tb_co2["year"] == 1991)][EMISSION_SOURCES].values
    )

    # Check that "emissions_total" agrees with the sum of emissions from individual sources.
    error = "The sum of all emissions should add up to total emissions (within 1%)."
    assert (
        abs(
            tb_co2.drop(columns=["country", "year", "emissions_total"]).sum(axis=1)
            - tb_co2["emissions_total"].fillna(0)
        )
        / (tb_co2["emissions_total"].fillna(0) + 1e-7)
        < 1e-2
    ).all(), error

    # Many rows have zero total emissions, but actually the individual sources are nan.
    # Total emissions in those cases should be nan, instead of zero.
    no_individual_emissions = tb_co2.drop(columns=["country", "year", "emissions_total"]).isnull().all(axis=1)
    tb_co2.loc[no_individual_emissions, "emissions_total"] = np.nan

    return tb_co2


def prepare_consumption_emissions(tb_consumption: Table) -> Table:
    """Prepare consumption-based emissions data (basic processing)."""
    # Select and rename columns.
    tb_consumption = tb_consumption[list(CONSUMPTION_EMISSIONS_COLUMNS)].rename(
        columns=CONSUMPTION_EMISSIONS_COLUMNS, errors="raise"
    )

    # Convert units from megatonnes of carbon per year emissions to tonnes of CO2 per year.
    for column in tb_consumption.drop(columns=["country", "year"]).columns:
        tb_consumption[column] *= MILLION_TONNES_OF_CARBON_TO_TONNES_OF_CO2

    # List indexes of rows in tb_consumption corresponding to outliers (defined above in OUTLIERS_IN_tb_consumption).
    outlier_indexes = [
        tb_consumption[(tb_consumption["country"] == outlier[0]) & (tb_consumption["year"] == outlier[1])].index.item()
        for outlier in OUTLIERS_IN_CONSUMPTION_DF
    ]

    error = (
        "Outliers were expected to have negative consumption emissions. "
        "Maybe outliers have been fixed (and should be removed from the code)."
    )
    assert (tb_consumption.loc[outlier_indexes]["consumption_emissions"] < 0).all(), error

    # Remove outliers.
    tb_consumption = tb_consumption.drop(outlier_indexes).reset_index(drop=True)

    return tb_consumption


def prepare_production_emissions(tb_production: Table) -> Table:
    """Prepare production-based emissions data (basic processing)."""
    # Convert units from megatonnes of carbon per year emissions to tonnes of CO2 per year.
    for column in tb_production.drop(columns=["country", "year"]).columns:
        tb_production[column] *= MILLION_TONNES_OF_CARBON_TO_TONNES_OF_CO2

    return tb_production


def prepare_land_use_emissions(tb_land_use: Table) -> Table:
    """Prepare land-use change emissions data (basic processing)."""
    # Convert units from megatonnes of carbon per year emissions to tonnes of CO2 per year.
    tb_land_use["emissions"] *= MILLION_TONNES_OF_CARBON_TO_TONNES_OF_CO2

    return tb_land_use


def prepare_historical_emissions(tb_historical: Table) -> Table:
    """Prepare historical emissions data."""
    # Select and rename columns from historical emissions data.
    tb_historical = tb_historical[list(HISTORICAL_EMISSIONS_COLUMNS)].rename(
        columns=HISTORICAL_EMISSIONS_COLUMNS, errors="raise"
    )

    # Convert units from gigatonnes of carbon per year emissions to tonnes of CO2 per year.
    for column in tb_historical.drop(columns=["country", "year"]).columns:
        tb_historical[column] *= BILLION_TONNES_OF_CARBON_TO_TONNES_OF_CO2

    return tb_historical


def extract_global_emissions(tb_co2: Table, tb_historical: Table, ds_population: Dataset) -> Table:
    """Extract World emissions by combining data from the Fossil CO2 emissions and the global emissions dataset.

    The resulting global emissions data includes bunker and land-use change emissions.

    NOTE: This function has to be used after selecting and renaming columns in tb_co2, but before harmonizing country
    names in tb_co2 (so that "International Transport" is still listed as a country).

    Parameters
    ----------
    tb_co2 : Table
        Production-based emissions from GCP's Fossil CO2 emissions dataset (csv file).
    tb_historical : Table
        Historical emissions from GCP's official global emissions dataset (excel file).
    ds_population : Dataset
        Population dataset.

    Returns
    -------
    global_emissions : Table
        World emissions.

    """
    # For some reason, "International Transport" is included as another country, that only has emissions from oil.
    # We separate it as another variable (only given at the global level).
    global_transport = tb_co2[tb_co2["country"] == INTERNATIONAL_TRANSPORT_LABEL].reset_index(drop=True)

    # Check that total emissions for international transport coincide with oil emissions.
    error = "Total emissions from international transport do not coincide with oil emissions."
    assert all((global_transport["emissions_from_oil"] - global_transport["emissions_total"]).dropna() == 0), error

    # Therefore, we can keep only one column for international transport emissions.
    global_transport = (
        global_transport[["year", "emissions_from_oil"]]
        .dropna()
        .rename(columns={"emissions_from_oil": "global_emissions_from_international_transport"}, errors="raise")
    )

    # Create a new table of global emissions.
    global_emissions = (
        tb_co2[tb_co2["country"].isin(["Global", "World"])][["year"] + EMISSION_SOURCES]
        .rename(columns={column: f"global_{column}" for column in EMISSION_SOURCES}, errors="raise")
        .sort_values("year")
        .reset_index(drop=True)
    )

    # Add bunker fuels to global emissions.
    global_emissions = pr.merge(global_emissions, global_transport, on=["year"], how="outer")

    # Add historical land-use change emissions to table of global emissions.
    global_emissions = pr.merge(
        global_emissions, tb_historical[["year", "global_emissions_from_land_use_change"]], how="left", on="year"
    )

    # Add variable of total emissions including fossil fuels and land use change.
    global_emissions["global_emissions_total_including_land_use_change"] = (
        global_emissions["global_emissions_total"] + global_emissions["global_emissions_from_land_use_change"]
    )

    # Calculate global cumulative emissions.
    for column in EMISSION_SOURCES + ["emissions_from_land_use_change", "emissions_total_including_land_use_change"]:
        global_emissions[f"global_cumulative_{column}"] = global_emissions[f"global_{column}"].cumsum()

    # Add a country column and add global population.
    global_emissions["country"] = "World"

    # Add global population.
    global_emissions = geo.add_population_to_table(
        tb=global_emissions, ds_population=ds_population, population_col="global_population"
    )

    return global_emissions


def harmonize_country_names(df: Table) -> Table:
    """Harmonize country names, and fix known issues with certain regions.

    Parameters
    ----------
    df : Table
        Emissions data (either from the fossil CO2, the production-based, consumption-based, or land-use emissions
        datasets).

    Returns
    -------
    df : Table
        Emissions data after harmonizing country names.

    """
    # Harmonize country names.
    df = geo.harmonize_countries(
        df=df,
        countries_file=paths.country_mapping_path,
        excluded_countries_file=paths.excluded_countries_path,
        warn_on_missing_countries=True,
        warn_on_unused_countries=False,
        make_missing_countries_nan=False,
        warn_on_unknown_excluded_countries=False,
    )

    # Check that there is only one data point for each country-year.
    # In the fossil CO2 emissions data, after harmonization, "Pacific Islands (Palau)" is mapped to "Palau", and
    # therefore there are rows with different data for the same country-year.
    # However, "Pacific Islands (Palau)" have data until 1991, and "Palau" has data from 1992 onwards.
    # After removing empty rows, there should be no overlap.
    columns_that_must_have_data = df.drop(columns=["country", "year"]).columns
    check = df.dropna(subset=columns_that_must_have_data, how="all").reset_index(drop=True)
    error = "After harmonizing country names, there is more than one data point for the same country-year."
    assert check[check.duplicated(subset=["country", "year"])].empty, error

    df = df.dropna(subset="country").reset_index(drop=True)

    return df


def combine_data_and_add_variables(
    tb_co2: Table,
    tb_production: Table,
    tb_consumption: Table,
    tb_global_emissions: Table,
    tb_land_use: Table,
    tb_energy: Table,
    ds_gdp: Dataset,
    ds_population: Table,
    ds_regions: Dataset,
    ds_income_groups: Dataset,
) -> Table:
    """Combine all relevant data into one table, add region aggregates, and add custom variables (e.g. emissions per
    capita).

    Parameters
    ----------
    tb_co2 : Table
        Production-based emissions from GCP's Fossil CO2 emissions dataset (csv file), after harmonization.
    tb_production : Table
        Production-based emissions from GCP's official national emissions dataset (excel file), after harmonization.
    tb_consumption : Table
        Consumption-based emissions from GCP's official national emissions dataset (excel file), after harmonization.
    tb_global_emissions : Table
        World emissions (including bunker and land-use change emissions).
    tb_land_use : Table
        National land-use change emissions from GCP's official dataset (excel file), after harmonization.
    tb_energy : Table
        Primary energy data.
    ds_gdp : Dataset
        GDP dataset.
    ds_population : Dataset
        Population dataset.
    ds_regions : Dataset
        Regions dataset.
    ds_income_groups : Dataset
        Income groups dataset.

    Returns
    -------
    tb_co2_with_regions : Table
        Combined data, with all additional variables and with region aggregates.

    """
    tb_co2_with_regions = tb_co2.copy()

    # Add region aggregates that were included in the national emissions file, but not in the Fossil CO2 emissions dataset.
    gcp_aggregates = sorted(set(tb_production["country"]) - set(tb_co2_with_regions["country"]))
    tb_co2_with_regions = pr.concat(
        [
            tb_co2_with_regions,
            tb_production[tb_production["country"].isin(gcp_aggregates)]
            .rename(columns={"production_emissions": "emissions_total"})
            .astype({"year": int}),
        ],
        ignore_index=True,
    ).reset_index(drop=True)

    # Add consumption emissions to main table (keep only the countries of the main table).
    # Given that additional GCP regions (e.g. "Africa (GCP)") have already been added to tb_co2
    # (when merging with tb_production), all countries from tb_consumption should be included in tb_co2.
    error = "Some countries in tb_consumption are not included in tb_co2."
    assert set(tb_consumption["country"]) < set(tb_co2_with_regions["country"]), error
    tb_co2_with_regions = pr.merge(tb_co2_with_regions, tb_consumption, on=["country", "year"], how="outer")

    # Add population to original table.
    tb_co2_with_regions = geo.add_population_to_table(
        tb=tb_co2_with_regions, ds_population=ds_population, warn_on_missing_countries=False
    )

    # Add GDP to main table.
    tb_co2_with_regions = geo.add_gdp_to_table(tb=tb_co2_with_regions, ds_gdp=ds_gdp)

    # Add primary energy to main table.
    tb_co2_with_regions = pr.merge(tb_co2_with_regions, tb_energy, on=["country", "year"], how="left")

    # For convenience, rename columns in land-use change emissions data.
    tb_land_use = tb_land_use.rename(
        columns={"emissions": "emissions_from_land_use_change", "quality_flag": "land_use_change_quality_flag"}
    )

    # Land-use change data does not include data for the World. Include it by merging with the global dataset.
    tb_land_use = pr.concat(
        [
            tb_land_use,
            tb_global_emissions.rename(
                columns={"global_emissions_from_land_use_change": "emissions_from_land_use_change"}
            )[["year", "emissions_from_land_use_change"]]
            .dropna()
            .assign(**{"country": "World"}),
        ],
        ignore_index=True,
    ).astype({"year": int})

    # Add land-use change emissions to main table.
    tb_co2_with_regions = pr.merge(tb_co2_with_regions, tb_land_use, on=["country", "year"], how="outer")

    # Add total emissions (including land-use change) for each country.
    tb_co2_with_regions["emissions_total_including_land_use_change"] = (
        tb_co2_with_regions["emissions_total"] + tb_co2_with_regions["emissions_from_land_use_change"]
    )

    # Create a copy of the current table, to be able to copy its metadata after adding region aggregates.
    _tb_co2_with_regions = tb_co2_with_regions.copy()

    # Add region aggregates.
    # Aggregate not only emissions data, but also population, gdp and primary energy.
    # This way we ensure that custom regions (e.g. "North America (excl. USA)") will have all required data.
    aggregations = {
        column: "sum"
        for column in tb_co2_with_regions.columns
        if column not in ["country", "year", "land_use_change_quality_flag"]
    }
    for region in REGIONS:
        countries_in_region = geo.list_members_of_region(
            region=region,
            ds_regions=ds_regions,
            ds_income_groups=ds_income_groups,
            additional_regions=REGIONS[region].get("additional_regions", None),
            excluded_regions=REGIONS[region].get("excluded_regions", None),
            additional_members=REGIONS[region].get("additional_members", None),
            excluded_members=REGIONS[region].get("excluded_members", None),
            include_historical_regions_in_income_groups=True,
        )
        tb_co2_with_regions = geo.add_region_aggregates(
            df=tb_co2_with_regions,
            region=region,
            countries_in_region=countries_in_region,
            countries_that_must_have_data=[],
            frac_allowed_nans_per_year=0.999,
            aggregations=aggregations,
        )

    # NOTE: The previous operation does not preserve metadata. Copy metadata of original table.
    tb_co2_with_regions = tb_co2_with_regions.copy_metadata(from_table=_tb_co2_with_regions)

    # Add global emissions and global cumulative emissions columns to main table.
    tb_co2_with_regions = pr.merge(
        tb_co2_with_regions, tb_global_emissions.drop(columns="country"), on=["year"], how="left"
    )

    # Ensure main table is sorted (so that cumulative emissions are properly calculated).
    tb_co2_with_regions = tb_co2_with_regions.sort_values(["country", "year"]).reset_index(drop=True)

    # Temporarily add certain global emissions variables.
    # This is done simply to be able to consider "consumption_emissions" as just another type of emission
    # when creating additional variables.
    tb_co2_with_regions["global_consumption_emissions"] = tb_co2_with_regions["global_emissions_total"]
    tb_co2_with_regions["global_cumulative_consumption_emissions"] = tb_co2_with_regions[
        "global_cumulative_emissions_total"
    ]

    # Add new variables for each source of emissions.
    for column in EMISSION_SOURCES + [
        "consumption_emissions",
        "emissions_from_land_use_change",
        "emissions_total_including_land_use_change",
    ]:
        # Add per-capita variables.
        tb_co2_with_regions[f"{column}_per_capita"] = tb_co2_with_regions[column] / tb_co2_with_regions["population"]

        # Add columns for cumulative emissions.
        # Rows that had nan emissions will have nan cumulative emissions.
        # But nans will not be propagated in the sum.
        # This means that countries with some (not all) nans will have the cumulative sum of the informed emissions
        # (treating nans as zeros), but will have nan on those rows that were not informed.
        # NOTE: Currently, this operation doesn't propagate metadata properly. This has to be done manually.
        tb_co2_with_regions[f"cumulative_{column}"] = (
            tb_co2_with_regions.groupby(["country"])[column].cumsum().copy_metadata(tb_co2_with_regions[column])
        )

        # Add share of global emissions.
        tb_co2_with_regions[f"{column}_as_share_of_global"] = (
            100 * tb_co2_with_regions[column] / tb_co2_with_regions[f"global_{column}"]
        )

        # Add share of global cumulative emissions.
        tb_co2_with_regions[f"cumulative_{column}_as_share_of_global"] = (
            100 * tb_co2_with_regions[f"cumulative_{column}"] / tb_co2_with_regions[f"global_cumulative_{column}"]
        )

    # Add total emissions per unit energy (in kg of emissions per kWh).
    tb_co2_with_regions["emissions_total_per_unit_energy"] = (
        TONNES_OF_CO2_TO_KG_OF_CO2
        * tb_co2_with_regions["emissions_total"]
        / (tb_co2_with_regions["primary_energy_consumption"] * TWH_TO_KWH)
    )

    # Add total emissions (including land-use change) per unit energy (in kg of emissions per kWh).
    tb_co2_with_regions["emissions_total_including_land_use_change_per_unit_energy"] = (
        TONNES_OF_CO2_TO_KG_OF_CO2
        * tb_co2_with_regions["emissions_total_including_land_use_change"]
        / (tb_co2_with_regions["primary_energy_consumption"] * TWH_TO_KWH)
    )

    # Add total emissions per unit GDP.
    tb_co2_with_regions["emissions_total_per_gdp"] = (
        TONNES_OF_CO2_TO_KG_OF_CO2 * tb_co2_with_regions["emissions_total"] / tb_co2_with_regions["gdp"]
    )

    # Add total emissions (including land-use change) per unit GDP.
    tb_co2_with_regions["emissions_total_including_land_use_change_per_gdp"] = (
        TONNES_OF_CO2_TO_KG_OF_CO2
        * tb_co2_with_regions["emissions_total_including_land_use_change"]
        / tb_co2_with_regions["gdp"]
    )

    # Add total consumption emissions per unit GDP.
    tb_co2_with_regions["consumption_emissions_per_gdp"] = (
        TONNES_OF_CO2_TO_KG_OF_CO2 * tb_co2_with_regions["consumption_emissions"] / tb_co2_with_regions["gdp"]
    )

    # Add variable of emissions embedded in trade.
    tb_co2_with_regions["traded_emissions"] = (
        tb_co2_with_regions["consumption_emissions"] - tb_co2_with_regions["emissions_total"]
    )
    tb_co2_with_regions["pct_traded_emissions"] = (
        100 * tb_co2_with_regions["traded_emissions"] / tb_co2_with_regions["emissions_total"]
    )
    tb_co2_with_regions["traded_emissions_per_capita"] = (
        tb_co2_with_regions["traded_emissions"] / tb_co2_with_regions["population"]
    )

    # Add variable of emissions embedded in trade, including land-use change emissions.
    tb_co2_with_regions["traded_emissions_including_land_use_change"] = (
        tb_co2_with_regions["consumption_emissions"] - tb_co2_with_regions["emissions_total_including_land_use_change"]
    )
    tb_co2_with_regions["pct_traded_emissions_including_land_use_change"] = (
        100
        * tb_co2_with_regions["traded_emissions_including_land_use_change"]
        / tb_co2_with_regions["emissions_total_including_land_use_change"]
    )
    tb_co2_with_regions["traded_emissions_including_land_use_change_per_capita"] = (
        tb_co2_with_regions["traded_emissions_including_land_use_change"] / tb_co2_with_regions["population"]
    )

    # Remove temporary columns.
    tb_co2_with_regions = tb_co2_with_regions.drop(
        columns=["global_consumption_emissions", "global_cumulative_consumption_emissions"]
    )

    # Add annual percentage growth of total emissions.
    # NOTE: Currently, this operation doesn't propagate metadata properly. This has to be done manually.
    tb_co2_with_regions["pct_growth_emissions_total"] = (
        tb_co2_with_regions.groupby("country")["emissions_total"].pct_change() * 100
    ).copy_metadata(tb_co2_with_regions["emissions_total"])

    # Add annual percentage growth of total emissions (including land-use change).
    # NOTE: Currently, this operation doesn't propagate metadata properly. This has to be done manually.
    tb_co2_with_regions["pct_growth_emissions_total_including_land_use_change"] = (
        tb_co2_with_regions.groupby("country")["emissions_total_including_land_use_change"].pct_change() * 100
    ).copy_metadata(tb_co2_with_regions["emissions_total_including_land_use_change"])

    # Add annual absolute growth of total emissions.
    tb_co2_with_regions["growth_emissions_total"] = tb_co2_with_regions.groupby("country")["emissions_total"].diff()

    # Add annual absolute growth of total emissions (including land-use change).
    tb_co2_with_regions["growth_emissions_total_including_land_use_change"] = tb_co2_with_regions.groupby("country")[
        "emissions_total_including_land_use_change"
    ].diff()

    # Create variable of population as a share of global population.
    tb_co2_with_regions["population_as_share_of_global"] = (
        tb_co2_with_regions["population"] / tb_co2_with_regions["global_population"] * 100
    )

    # Replace infinity values (for example when calculating growth from zero to non-zero) in the data by nan.
    for column in tb_co2_with_regions.drop(columns=["country", "year"]).columns:
        tb_co2_with_regions.loc[np.isinf(tb_co2_with_regions[column]), column] = np.nan

    # For special GCP countries/regions (e.g. "Africa (GCP)") we should keep only the original data.
    # Therefore, make nan all additional variables for those countries/regions, and keep only GCP's original data.
    added_variables = tb_co2_with_regions.drop(
        columns=["country", "year"] + COLUMNS_THAT_MUST_HAVE_DATA
    ).columns.tolist()
    tb_co2_with_regions.loc[
        (tb_co2_with_regions["country"].str.contains(" (GCP)", regex=False)), added_variables
    ] = np.nan

    # Remove uninformative rows (those that have only data for, say, gdp, but not for variables related to emissions).
    tb_co2_with_regions = tb_co2_with_regions.dropna(subset=COLUMNS_THAT_MUST_HAVE_DATA, how="all").reset_index(
        drop=True
    )

    # Set an appropriate index, ensure there are no rows that only have nan, and sort conveniently.
    tb_co2_with_regions = tb_co2_with_regions.set_index(["country", "year"], verify_integrity=True)
    tb_co2_with_regions = (
        tb_co2_with_regions.dropna(subset=tb_co2_with_regions.columns, how="all").sort_index().sort_index(axis=1)
    )

    # Rename table.
    tb_co2_with_regions.metadata.short_name = paths.short_name

    return tb_co2_with_regions


def run(dest_dir: str) -> None:
    #
    # Load inputs.
    #
    # Load meadow dataset and read all its tables.
    ds_meadow = paths.load_dataset("global_carbon_budget")
    tb_co2 = ds_meadow["global_carbon_budget_fossil_co2_emissions"].reset_index()
    tb_historical = ds_meadow["global_carbon_budget_historical_budget"].reset_index()
    tb_consumption = ds_meadow["global_carbon_budget_consumption_emissions"].reset_index()
    tb_production = ds_meadow["global_carbon_budget_production_emissions"].reset_index()
    tb_land_use = ds_meadow["global_carbon_budget_land_use_change"].reset_index()

    # Load primary energy consumption dataset and read its main table.
    ds_energy = paths.load_dataset("primary_energy_consumption")
    tb_energy = ds_energy["primary_energy_consumption"].reset_index()

    ####################################################################################################################
    # TODO: Remove this temporary solution once primary energy consumption dataset has origins.
    error = "Remove temporary solution now that primary energy consumption has origins."
    assert not tb_energy["primary_energy_consumption__twh"].metadata.origins, error
    from owid.catalog import License, Origin

    tb_energy["primary_energy_consumption__twh"].metadata.sources = []
    tb_energy["primary_energy_consumption__twh"].metadata.origins = [
        Origin(
            producer="Energy Institute",
            title="Statistical Review of World Energy",
            attribution="Energy Institute - Statistical Review of World Energy (2023)",
            url_main="https://www.energyinst.org/statistical-review/",
            url_download="https://www.energyinst.org/__data/assets/file/0007/1055761/Consolidated-Dataset-Panel-format-CSV.csv",
            date_published="2023-06-26",
            date_accessed="2023-06-27",
            description="The Energy Institute Statistical Review of World Energy analyses data on world energy markets from the prior year. Previously produced by BP, the Review has been providing timely, comprehensive and objective data to the energy community since 1952.",
            license=License(
                name="©Energy Institute 2023",
                url="https://www.energyinst.org/__data/assets/file/0007/1055761/Consolidated-Dataset-Panel-format-CSV.csv",
            ),
        ),
        Origin(
            producer="U.S. Energy Information Administration",
            title="International Energy Data",
            url_main="https://www.eia.gov/opendata/bulkfiles.php",
            url_download="https://api.eia.gov/bulk/INTL.zip",
            date_published="2023-06-27",
            date_accessed="2023-07-10",
            license=License(name="Public domain", url="https://www.eia.gov/about/copyrights_reuse.php"),
        ),
    ]

    ####################################################################################################################

    # Load GDP dataset.
    ds_gdp = paths.load_dataset("ggdc_maddison")

    # Load population dataset.
    ds_population = paths.load_dataset("population")

    # Load regions dataset.
    ds_regions = paths.load_dataset("regions")

    # Load income groups dataset.
    ds_income_groups = paths.load_dataset("income_groups")

    #
    # Process data.
    #
    # Prepare fossil CO2 emissions data.
    tb_co2 = prepare_fossil_co2_emissions(tb_co2=tb_co2)

    # Prepare consumption-based emission data.
    tb_consumption = prepare_consumption_emissions(tb_consumption=tb_consumption)

    # Prepare production-based emission data.
    tb_production = prepare_production_emissions(tb_production=tb_production)

    # Prepare land-use emission data.
    tb_land_use = prepare_land_use_emissions(tb_land_use=tb_land_use)

    # Select and rename columns from primary energy data.
    tb_energy = tb_energy[list(PRIMARY_ENERGY_COLUMNS)].rename(columns=PRIMARY_ENERGY_COLUMNS, errors="raise")

    # Prepare historical emissions data.
    tb_historical = prepare_historical_emissions(tb_historical=tb_historical)

    # Run sanity checks on input data.
    sanity_checks_on_input_data(
        tb_production=tb_production, tb_consumption=tb_consumption, tb_historical=tb_historical, tb_co2=tb_co2
    )

    # For some reason, "International Transport" is included as another country, that only has emissions from oil.
    # Extract that data and remove it from the rest of national emissions.
    tb_global_emissions = extract_global_emissions(
        tb_co2=tb_co2, tb_historical=tb_historical, ds_population=ds_population
    )

    # Harmonize country names.
    tb_co2 = harmonize_country_names(df=tb_co2)
    tb_consumption = harmonize_country_names(df=tb_consumption)
    tb_production = harmonize_country_names(df=tb_production)
    tb_land_use = harmonize_country_names(df=tb_land_use)

    # Add new variables to main table (consumption-based emissions, emission intensity, per-capita emissions, etc.).
    tb_combined = combine_data_and_add_variables(
        tb_co2=tb_co2,
        tb_production=tb_production,
        tb_consumption=tb_consumption,
        tb_global_emissions=tb_global_emissions,
        tb_land_use=tb_land_use,
        tb_energy=tb_energy,
        ds_gdp=ds_gdp,
        ds_population=ds_population,
        ds_regions=ds_regions,
        ds_income_groups=ds_income_groups,
    )

    # Run sanity checks on output data.
    sanity_checks_on_output_data(tb_combined)

    #
    # Save outputs.
    #
    # Create a new garden dataset and use metadata from meadow dataset.
    ds_garden = create_dataset(
        dest_dir=dest_dir, tables=[tb_combined], default_metadata=ds_meadow.metadata, check_variables_metadata=True
    )
    ds_garden.save()
