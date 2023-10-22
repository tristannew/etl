"""Load a meadow dataset and create a garden dataset."""

from etl.data_helpers import geo
from etl.helpers import PathFinder, create_dataset

# Get paths and naming conventions for current step.
paths = PathFinder(__file__)


def run(dest_dir: str) -> None:
    #
    # Load inputs.
    #
    # Load meadow dataset.
    ds_meadow = paths.load_dataset("state_capacity_dataset")

    # Read table from meadow dataset.
    tb = ds_meadow["state_capacity_dataset"].reset_index()

    #
    # Process data.

    # Drop columns
    drop_list = ["cntrynum", "iso3", "iso2", "ccode", "scode", "vdem", "wbregion", "sample_polity"]
    tb = tb.drop(columns=drop_list)

    # Convert tax indicators to percentages.
    tax_vars = ["tax_inc_tax", "tax_trade_tax", "taxrev_gdp"]
    tb[tax_vars] *= 100

    #
    tb = geo.harmonize_countries(
        df=tb,
        countries_file=paths.country_mapping_path,
    )
    tb = tb.set_index(["country", "year"], verify_integrity=True)

    #
    # Save outputs.
    #
    # Create a new garden dataset with the same metadata as the meadow dataset.
    ds_garden = create_dataset(
        dest_dir, tables=[tb], check_variables_metadata=True, default_metadata=ds_meadow.metadata
    )

    # Save changes in the new garden dataset.
    ds_garden.save()