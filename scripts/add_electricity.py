# SPDX-FileCopyrightText: : 2017-2020 The PyPSA-Eur Authors, 2021 PyPSA-Africa Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later
# coding: utf-8
"""
Adds electrical generators, load and existing hydro storage units to a base network.

Relevant Settings
-----------------

.. code:: yaml

    costs:
        year:
        USD2013_to_EUR2013:
        dicountrate:
        emission_prices:

    electricity:
        max_hours:
        marginal_cost:
        capital_cost:
        conventional_carriers:
        co2limit:
        extendable_carriers:
        include_renewable_capacities_from_OPSD:
        estimate_renewable_capacities_from_capacity_stats:

    load:
        scale:
        ssp:
        weather_year:
        prediction_year:
        region_load:


    renewable:
        hydro:
            carriers:
            hydro_max_hours:
            hydro_capital_cost:

    lines:
        length_factor:

.. seealso::
    Documentation of the configuration file ``config.yaml`` at :ref:`costs_cf`,
    :ref:`electricity_cf`, :ref:`load_cf`, :ref:`renewable_cf`, :ref:`lines_cf`

Inputs
------

- ``data/costs.csv``: The database of cost assumptions for all included technologies for specific years from various sources; e.g. discount rate, lifetime, investment (CAPEX), fixed operation and maintenance (FOM), variable operation and maintenance (VOM), fuel costs, efficiency, carbon-dioxide intensity.
- ``data/bundle/hydro_capacities.csv``: Hydropower plant store/discharge power capacities, energy storage capacity, and average hourly inflow by country.  Not currently used!

    .. image:: ../img/hydrocapacities.png
        :scale: 34 %

- ``data/geth2015_hydro_capacities.csv``: alternative to capacities above; not currently used!
- ``resources/ssp2-2.6/2030/era5_2013/Africa.nc`` Hourly country load profiles produced by GEGIS
- ``resources/regions_onshore.geojson``: confer :ref:`busregions`
- ``resources/gadm_shapes.geojson``: confer :ref:`shapes`
- ``resources/powerplants.csv``: confer :ref:`powerplants`
- ``resources/profile_{}.nc``: all technologies in ``config["renewables"].keys()``, confer :ref:`renewableprofiles`.
- ``networks/base.nc``: confer :ref:`base`

Outputs
-------

- ``networks/elec.nc``:

    .. image:: ../img/elec.png
            :scale: 33 %

Description
-----------

The rule :mod:`add_electricity` ties all the different data inputs from the preceding rules together into a detailed PyPSA network that is stored in ``networks/elec.nc``. It includes:

- today's transmission topology and transfer capacities (in future, optionally including lines which are under construction according to the config settings ``lines: under_construction`` and ``links: under_construction``),
- today's thermal and hydro power generation capacities (for the technologies listed in the config setting ``electricity: conventional_carriers``), and
- today's load time-series (upsampled in a top-down approach according to population and gross domestic product)

It further adds extendable ``generators`` with **zero** capacity for

- photovoltaic, onshore and AC- as well as DC-connected offshore wind installations with today's locational, hourly wind and solar capacity factors (but **no** current capacities),
- additional open- and combined-cycle gas turbines (if ``OCGT`` and/or ``CCGT`` is listed in the config setting ``electricity: extendable_carriers``)
"""
import logging
import os

import geopandas as gpd
import numpy as np
import pandas as pd
import powerplantmatching as pm
import pypsa
import xarray as xr
from _helpers import configure_logging
from _helpers import update_p_nom_max
from powerplantmatching.export import map_country_bus
from shapely.validation import make_valid
from vresutils import transfer as vtransfer
from vresutils.costdata import annuity
from vresutils.load import timeseries_opsd

idx = pd.IndexSlice

logger = logging.getLogger(__name__)


def normed(s):
    return s / s.sum()


def _add_missing_carriers_from_costs(n, costs, carriers):
    missing_carriers = pd.Index(carriers).difference(n.carriers.index)
    if missing_carriers.empty:
        return

    emissions_cols = (costs.columns.to_series().
                      loc[lambda s: s.str.endswith("_emissions")].values)
    suptechs = missing_carriers.str.split("-").str[0]
    emissions = costs.loc[suptechs, emissions_cols].fillna(0.0)
    emissions.index = missing_carriers
    n.import_components_from_dataframe(emissions, "Carrier")


def load_costs(Nyears=1.0, tech_costs=None, config=None, elec_config=None):
    if tech_costs is None:
        tech_costs = snakemake.input.tech_costs

    if config is None:
        config = snakemake.config["costs"]

    # set all asset costs and other parameters
    costs = pd.read_csv(tech_costs, index_col=list(range(3))).sort_index()

    # correct units to MW and EUR
    costs.loc[costs.unit.str.contains("/kW"), "value"] *= 1e3
    costs.loc[costs.unit.str.contains("USD"),
              "value"] *= config["USD2013_to_EUR2013"]

    costs = (costs.loc[idx[:, config["year"], :], "value"].unstack(
        level=2).groupby("technology").sum(min_count=1))

    costs = costs.fillna({
        "CO2 intensity": 0,
        "FOM": 0,
        "VOM": 0,
        "discount rate": config["discountrate"],
        "efficiency": 1,
        "fuel": 0,
        "investment": 0,
        "lifetime": 25,
    })

    costs["capital_cost"] = (
        (annuity(costs["lifetime"], costs["discount rate"]) +
         costs["FOM"] / 100.0) * costs["investment"] * Nyears)

    costs.at["OCGT", "fuel"] = costs.at["gas", "fuel"]
    costs.at["CCGT", "fuel"] = costs.at["gas", "fuel"]

    costs["marginal_cost"] = costs["VOM"] + costs["fuel"] / costs["efficiency"]

    costs = costs.rename(columns={"CO2 intensity": "co2_emissions"})

    costs.at["OCGT", "co2_emissions"] = costs.at["gas", "co2_emissions"]
    costs.at["CCGT", "co2_emissions"] = costs.at["gas", "co2_emissions"]

    costs.at[
        "solar",
        "capital_cost"] = 0.5 * (costs.at["solar-rooftop", "capital_cost"] +
                                 costs.at["solar-utility", "capital_cost"])

    def costs_for_storage(store, link1, link2=None, max_hours=1.0):
        capital_cost = link1["capital_cost"] + max_hours * store["capital_cost"]
        if link2 is not None:
            capital_cost += link2["capital_cost"]
        return pd.Series(
            dict(capital_cost=capital_cost,
                 marginal_cost=0.0,
                 co2_emissions=0.0))

    if elec_config is None:
        elec_config = snakemake.config["electricity"]
    max_hours = elec_config["max_hours"]
    costs.loc["battery"] = costs_for_storage(
        costs.loc["battery storage"],
        costs.loc["battery inverter"],
        max_hours=max_hours["battery"],
    )
    costs.loc["H2"] = costs_for_storage(
        costs.loc["hydrogen storage"],
        costs.loc["fuel cell"],
        costs.loc["electrolysis"],
        max_hours=max_hours["H2"],
    )

    for attr in ("marginal_cost", "capital_cost"):
        overwrites = config.get(attr)
        if overwrites is not None:
            overwrites = pd.Series(overwrites)
            costs.loc[overwrites.index, attr] = overwrites

    return costs


def load_powerplants(ppl_fn=None):
    if ppl_fn is None:
        ppl_fn = snakemake.input.powerplants
    carrier_dict = {
        "ocgt": "OCGT",
        "ccgt": "CCGT",
        "bioenergy": "biomass",
        "ccgt, thermal": "CCGT",
        "hard coal": "coal",
    }
    return (pd.read_csv(ppl_fn, index_col=0, dtype={
        "bus": "str"
    }).powerplant.to_pypsa_names().rename(columns=str.lower).drop(
        columns=["efficiency"]).replace({"carrier": carrier_dict}))


def attach_load(
    n,
    regions,
    weather_year,
    prediction_year,
    region_load,
    ssp,
    admin_shapes,
    countries,
    scale,
):
    """
    Add load to the network and distributes them according GDP and population.

    Parameters
    ----------
    n : pypsa network
    regions : .geojson
        Contains bus_id of low voltage substations and
        bus region shapes (voronoi cells)
        weather_year: weather year to consider when defining the load (different renewable potentials)
        prediction_year: prediction year to consider when defining the load (different GDP, population)
        region_load: world region to consider when defining the load
        ssp: shared socio-economic pathway (GDP and population growth) scenario to consider when defining the load
    load : .nc
        Contains timeseries of load data per country
    admin_shapes : .geojson
        contains subregional gdp, population and shape data
    countries : list
        List of countries that is config input
    scale : float
        The scale factor is multiplied with the load (1.3 = 30% more load)

    Returns
    -------
    n : pypsa network
        Now attached with load time series
    """
    substation_lv_i = n.buses.index[n.buses["substation_lv"]]
    regions = (
        gpd.read_file(regions).set_index("name").reindex(substation_lv_i)
    ).dropna(
        axis="rows")  # TODO: check if dropna required here. NaN shapes exist?

    cwd_path = os.path.dirname(os.getcwd())
    load_path = os.path.join(
        cwd_path,
        "resources",
        str(ssp),
        str(prediction_year),
        "era5_" + str(weather_year),
        str(region_load) + ".nc",
    )
    gegis_load = xr.open_dataset(load_path)
    gegis_load = gegis_load.to_dataframe().reset_index().set_index("time")
    # filter load for analysed countries
    gegis_load = gegis_load.loc[gegis_load.region_code.isin(countries)]
    logger.info(f"Load data scaled with scalling factor {scale}.")
    gegis_load *= scale
    shapes = gpd.read_file(admin_shapes).set_index("GADM_ID")
    shapes.loc[:,
               "geometry"] = shapes["geometry"].apply(lambda x: make_valid(x))

    def upsample(cntry, group):
        """
        Distributes load in country according to population and gdp
        """
        l = gegis_load.loc[gegis_load.region_code ==
                           cntry]["Electricity demand"]
        if len(group) == 1:
            return pd.DataFrame({group.index[0]: l})
        else:
            shapes_cntry = shapes.loc[shapes.country == cntry]
            transfer = vtransfer.Shapes2Shapes(group,
                                               shapes_cntry.geometry,
                                               normed=False).T.tocsr()
            gdp_n = pd.Series(transfer.dot(
                shapes_cntry["gdp"].fillna(1.0).values),
                              index=group.index)
            pop_n = pd.Series(transfer.dot(
                shapes_cntry["pop"].fillna(1.0).values),
                              index=group.index)

            # relative factors 0.6 and 0.4 have been determined from a linear
            # regression on the country to EU continent load data
            # (refer to vresutils.load._upsampling_weights)
            # TODO: require adjustment for Africa
            factors = normed(0.6 * normed(gdp_n) + 0.4 * normed(pop_n))
            return pd.DataFrame(
                factors.values * l.values[:, np.newaxis],
                index=l.index,
                columns=factors.index,
            )

    load = pd.concat(
        [
            upsample(cntry, group)
            for cntry, group in regions.geometry.groupby(regions.country)
        ],
        axis=1,
    )

    n.madd("Load", substation_lv_i, bus=substation_lv_i, p_set=load)


def update_transmission_costs(n,
                              costs,
                              length_factor=1.0,
                              simple_hvdc_costs=False):
    n.lines["capital_cost"] = (n.lines["length"] * length_factor *
                               costs.at["HVAC overhead", "capital_cost"])

    if n.links.empty:
        return

    dc_b = n.links.carrier == "DC"
    # If there are no "DC" links, then the 'underwater_fraction' column
    # may be missing. Therefore we have to return here.
    # TODO: Require fix
    if n.links.loc[n.links.carrier == "DC"].empty:
        return

    if simple_hvdc_costs:
        costs = (n.links.loc[dc_b, "length"] * length_factor *
                 costs.at["HVDC overhead", "capital_cost"])
    else:
        costs = (n.links.loc[dc_b, "length"] * length_factor *
                 ((1.0 - n.links.loc[dc_b, "underwater_fraction"]) *
                  costs.at["HVDC overhead", "capital_cost"] +
                  n.links.loc[dc_b, "underwater_fraction"] *
                  costs.at["HVDC submarine", "capital_cost"]) +
                 costs.at["HVDC inverter pair", "capital_cost"])
    n.links.loc[dc_b, "capital_cost"] = costs


def attach_wind_and_solar(n, costs):
    for tech in snakemake.config["renewable"]:
        if tech == "hydro":
            continue

        ren_config = snakemake.config["renewable"][tech]

        extendable = False  # set by default false and update below
        if "extendable" in ren_config:
            extendable = ren_config["extendable"]

        n.add("Carrier", name=tech)

        with xr.open_dataset(getattr(snakemake.input,
                                     "profile_" + tech)) as ds:

            if ds.indexes["bus"].empty:
                continue

            suptech = tech.split("-", 2)[0]
            if suptech == "offwind":
                continue
                # TODO: Uncomment out and debug.
                # underwater_fraction = ds["underwater_fraction"].to_pandas()
                # connection_cost = (
                #     snakemake.config["lines"]["length_factor"] *
                #     ds["average_distance"].to_pandas() *
                #     (underwater_fraction *
                #      costs.at[tech + "-connection-submarine", "capital_cost"] +
                #      (1.0 - underwater_fraction) *
                #      costs.at[tech + "-connection-underground", "capital_cost"]
                #      ))
                # capital_cost = (costs.at["offwind", "capital_cost"] +
                #                 costs.at[tech + "-station", "capital_cost"] +
                #                 connection_cost)
                # logger.info(
                #     "Added connection cost of {:0.0f}-{:0.0f} Eur/MW/a to {}".
                #     format(connection_cost.min(), connection_cost.max(), tech))
            else:
                capital_cost = costs.at[tech, "capital_cost"]

            n.madd(
                "Generator",
                ds.indexes["bus"],
                " " + tech,
                bus=ds.indexes["bus"],
                carrier=tech,
                p_nom_extendable=extendable,
                p_nom_max=ds["p_nom_max"].to_pandas(),
                weight=ds["weight"].to_pandas(),
                marginal_cost=costs.at[suptech, "marginal_cost"],
                capital_cost=capital_cost,
                efficiency=costs.at[suptech, "efficiency"],
                p_max_pu=ds["profile"].transpose("time", "bus").to_pandas(),
            )


def attach_conventional_generators(n, costs, ppl):
    carriers = snakemake.config["electricity"]["conventional_carriers"]

    _add_missing_carriers_from_costs(n, costs, carriers)

    ppl = (ppl.query("carrier in @carriers").join(
        costs, on="carrier").rename(index=lambda s: "C" + str(s)))

    logger.info("Adding {} generators with capacities [MW] \n{}".format(
        len(ppl),
        ppl.groupby("carrier").p_nom.sum()))

    n.madd(
        "Generator",
        ppl.index,
        carrier=ppl.carrier,
        bus=ppl.bus,
        p_nom=ppl.p_nom,
        efficiency=ppl.efficiency,
        marginal_cost=ppl.marginal_cost,
        capital_cost=0,
    )

    logger.warning(
        f"Capital costs for conventional generators put to 0 EUR/MW.")


def attach_hydro(n, costs, ppl):
    if "hydro" not in snakemake.config["renewable"]:
        return
    c = snakemake.config["renewable"]["hydro"]
    carriers = c.get("carriers", ["ror", "PHS", "hydro"])

    _add_missing_carriers_from_costs(n, costs, carriers)

    ppl = (ppl.query('carrier == "hydro"').reset_index(drop=True).rename(
        index=lambda s: str(s) + " hydro"))

    # TODO: remove this line to address nan when powerplantmatching is stable
    # Current fix, NaN technologies set to ROR
    ppl.loc[ppl.technology.isna(), "technology"] = "Run-Of-River"

    ror = ppl.query('technology == "Run-Of-River"')
    phs = ppl.query('technology == "Pumped Storage"')
    hydro = ppl.query('technology == "Reservoir"')

    bus_id = ppl["bus"]

    inflow_idx = ror.index.union(hydro.index)
    if not inflow_idx.empty:
        with xr.open_dataarray(snakemake.input.profile_hydro) as inflow:
            inflow_stations = pd.Index(bus_id[inflow_idx])
            missing_c = inflow_stations.unique().difference(
                inflow.indexes["plant"])
            assert missing_c.empty, (
                f"'{snakemake.input.profile_hydro}' is missing "
                f"inflow time-series for at least one bus: {', '.join(missing_c)}"
            )

            inflow_t = (inflow.sel(plant=inflow_stations).rename({
                "plant":
                "name"
            }).assign_coords(name=inflow_idx).transpose("time",
                                                        "name").to_pandas())

    if "ror" in carriers and not ror.empty:
        n.madd(
            "Generator",
            ror.index,
            carrier="ror",
            bus=ror["bus"],
            p_nom=ror["p_nom"],
            efficiency=costs.at["ror", "efficiency"],
            capital_cost=costs.at["ror", "capital_cost"],
            weight=ror["p_nom"],
            p_max_pu=(inflow_t[ror.index].divide(ror["p_nom"], axis=1).where(
                lambda df: df <= 1.0, other=1.0)),
        )

    if "PHS" in carriers and not phs.empty:
        # fill missing max hours to config value and
        # assume no natural inflow due to lack of data
        phs = phs.replace({"max_hours": {0: c["PHS_max_hours"]}})
        n.madd(
            "StorageUnit",
            phs.index,
            carrier="PHS",
            bus=phs["bus"],
            p_nom=phs["p_nom"],
            capital_cost=costs.at["PHS", "capital_cost"],
            max_hours=phs["max_hours"],
            efficiency_store=np.sqrt(costs.at["PHS", "efficiency"]),
            efficiency_dispatch=np.sqrt(costs.at["PHS", "efficiency"]),
            cyclic_state_of_charge=True,
        )

    if "hydro" in carriers and not hydro.empty:
        hydro_max_hours = c.get("hydro_max_hours")
        hydro_stats = pd.read_csv(snakemake.input.hydro_capacities,
                                  comment="#",
                                  na_values="-",
                                  index_col=0)
        e_target = hydro_stats["E_store[TWh]"].clip(lower=0.2) * 1e6
        e_installed = hydro.eval("p_nom * max_hours").groupby(
            hydro.country).sum()
        e_missing = e_target - e_installed
        missing_mh_i = hydro.query("max_hours == 0").index

        if hydro_max_hours == "energy_capacity_totals_by_country":
            max_hours_country = (
                e_missing /
                hydro.loc[missing_mh_i].groupby("country").p_nom.sum())

        elif hydro_max_hours == "estimate_by_large_installations":
            max_hours_country = (hydro_stats["E_store[TWh]"] * 1e3 /
                                 hydro_stats["p_nom_discharge[GW]"])

        missing_countries = pd.Index(hydro["country"].unique()).difference(
            max_hours_country.dropna().index)
        if not missing_countries.empty:
            logger.warning(
                "Assuming max_hours=6 for hydro reservoirs in the countries: {}"
                .format(", ".join(missing_countries)))
        hydro_max_hours = hydro.max_hours.where(
            hydro.max_hours > 0,
            hydro.country.map(max_hours_country)).fillna(6)

        n.madd(
            "StorageUnit",
            hydro.index,
            carrier="hydro",
            bus=hydro["bus"],
            p_nom=hydro["p_nom"],
            max_hours=hydro_max_hours,
            capital_cost=(costs.at["hydro", "capital_cost"]
                          if c.get("hydro_capital_cost") else 0.0),
            marginal_cost=costs.at["hydro", "marginal_cost"],
            p_max_pu=1.0,  # dispatch
            p_min_pu=0.0,  # store
            efficiency_dispatch=costs.at["hydro", "efficiency"],
            efficiency_store=0.0,
            cyclic_state_of_charge=True,
            inflow=inflow_t.loc[:, hydro.index],
        )


def attach_extendable_generators(n, costs, ppl):
    elec_opts = snakemake.config["electricity"]
    carriers = pd.Index(elec_opts["extendable_carriers"]["Generator"])

    _add_missing_carriers_from_costs(n, costs, carriers)

    for tech in carriers:
        if tech.startswith("OCGT"):
            ocgt = (ppl.query("carrier in ['OCGT', 'CCGT']").groupby(
                "bus", as_index=False).first())
            n.madd(
                "Generator",
                ocgt.index,
                suffix=" OCGT",
                bus=ocgt["bus"],
                carrier=tech,
                p_nom_extendable=True,
                p_nom=0.0,
                capital_cost=costs.at["OCGT", "capital_cost"],
                marginal_cost=costs.at["OCGT", "marginal_cost"],
                efficiency=costs.at["OCGT", "efficiency"],
            )

        elif tech.startswith("CCGT"):
            ccgt = (ppl.query("carrier in ['OCGT', 'CCGT']").groupby(
                "bus", as_index=False).first())
            n.madd(
                "Generator",
                ccgt.index,
                suffix=" CCGT",
                bus=ccgt["bus"],
                carrier=tech,
                p_nom_extendable=True,
                p_nom=0.0,
                capital_cost=costs.at["CCGT", "capital_cost"],
                marginal_cost=costs.at["CCGT", "marginal_cost"],
                efficiency=costs.at["CCGT", "efficiency"],
            )

        elif tech.startswith("nuclear"):
            nuclear = (ppl.query("carrier == 'nuclear'").groupby(
                "bus", as_index=False).first())
            n.madd(
                "Generator",
                nuclear.index,
                suffix=" nuclear",
                bus=nuclear["bus"],
                carrier=tech,
                p_nom_extendable=True,
                p_nom=0.0,
                capital_cost=costs.at["nuclear", "capital_cost"],
                marginal_cost=costs.at["nuclear", "marginal_cost"],
                efficiency=costs.at["nuclear", "efficiency"],
            )

        else:
            raise NotImplementedError(
                f"Adding extendable generators for carrier "
                "'{tech}' is not implemented, yet. "
                "Only OCGT, CCGT and nuclear are allowed at the moment.")


def attach_OPSD_renewables(n):

    available = ["DE", "FR", "PL", "CH", "DK", "CZ", "SE", "GB"]
    tech_map = {"Onshore": "onwind", "Offshore": "offwind", "Solar": "solar"}
    countries = set(available) & set(n.buses.country)
    techs = snakemake.config["electricity"].get(
        "renewable_capacities_from_OPSD", [])
    tech_map = {k: v for k, v in tech_map.items() if v in techs}

    if not tech_map:
        return

    logger.info(f'Using OPSD renewable capacities in {", ".join(countries)} '
                f'for technologies {", ".join(tech_map.values())}.')

    df = pd.concat([pm.data.OPSD_VRE_country(c) for c in countries])
    technology_b = ~df.Technology.isin(["Onshore", "Offshore"])
    df["Fueltype"] = df.Fueltype.where(technology_b, df.Technology)
    df = df.query(
        "Fueltype in @tech_map").powerplant.convert_country_to_alpha2()

    for fueltype, carrier_like in tech_map.items():
        gens = n.generators[lambda df: df.carrier.str.contains(carrier_like)]
        buses = n.buses.loc[gens.bus.unique()]
        gens_per_bus = gens.groupby("bus").p_nom.count()

        caps = map_country_bus(df.query("Fueltype == @fueltype"), buses)
        caps = caps.groupby(["bus"]).Capacity.sum()
        caps = caps / gens_per_bus.reindex(caps.index, fill_value=1)

        n.generators.p_nom.update(gens.bus.map(caps).dropna())
        n.generators.p_nom_min.update(gens.bus.map(caps).dropna())


def estimate_renewable_capacities(n, tech_map=None):
    if tech_map is None:
        tech_map = snakemake.config["electricity"].get(
            "estimate_renewable_capacities_from_capacity_stats", {})

    if len(tech_map) == 0:
        return

    capacities = (
        pm.data.Capacity_stats().powerplant.convert_country_to_alpha2()
        [lambda df: df.Energy_Source_Level_2].set_index(
            ["Fueltype", "Country"]).sort_index())

    countries = n.buses.country.unique()

    if len(countries) == 0:
        return

    logger.info(
        "heuristics applied to distribute renewable capacities [MW] \n{}".
        format(
            capacities.query("Fueltype in @tech_map.keys() and Capacity >= 0.1"
                             ).groupby("Country").agg({"Capacity": "sum"})))

    for ppm_fueltype, techs in tech_map.items():
        tech_capacities = capacities.loc[ppm_fueltype,
                                         "Capacity"].reindex(countries,
                                                             fill_value=0.0)
        tech_i = n.generators.query("carrier in @techs")[n.generators.query(
            "carrier in @techs").bus.map(
                n.buses.country).isin(countries)].index
        n.generators.loc[tech_i, "p_nom"] = (
            (n.generators_t.p_max_pu[tech_i].mean() *
             n.generators.loc[tech_i, "p_nom_max"]
             )  # maximal yearly generation
            .groupby(n.generators.bus.map(n.buses.country)).transform(
                lambda s: normed(s) * tech_capacities.at[s.name]).where(
                    lambda s: s > 0.1, 0.0))  # only capacities above 100kW
        n.generators.loc[tech_i, "p_nom_min"] = n.generators.loc[tech_i,
                                                                 "p_nom"]


def add_nice_carrier_names(n, config=None):
    if config is None:
        config = snakemake.config
    carrier_i = n.carriers.index
    nice_names = (pd.Series(
        config["plotting"]["nice_names"]).reindex(carrier_i).fillna(
            carrier_i.to_series().str.title()))
    n.carriers["nice_name"] = nice_names
    colors = pd.Series(config["plotting"]["tech_colors"]).reindex(carrier_i)
    if colors.isna().any():
        missing_i = list(colors.index[colors.isna()])
        logger.warning(f"tech_colors for carriers {missing_i} not defined "
                       "in config.")
    n.carriers["color"] = colors


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        os.chdir(os.path.dirname(os.path.abspath(__file__)))

        snakemake = mock_snakemake("add_electricity")
    configure_logging(snakemake)

    n = pypsa.Network(snakemake.input.base_network)
    Nyears = n.snapshot_weightings.objective.sum() / 8760.0

    # Snakemake imports:
    regions = snakemake.input.regions

    countries = create_country_list(snakemake.config["countries"])
    weather_year = snakemake.config["load_options"]["weather_year"]
    prediction_year = snakemake.config["load_options"]["prediction_year"]
    region_load = snakemake.config["load_options"]["region_load"]
    ssp = snakemake.config["load_options"]["ssp"]
    scale = snakemake.config["load_options"]["scale"]
    admin_shapes = snakemake.input.gadm_shapes

    costs = load_costs(Nyears)
    ppl = load_powerplants()

    attach_load(
        n,
        regions,
        weather_year,
        prediction_year,
        region_load,
        ssp,
        admin_shapes,
        countries,
        scale,
    )
    update_transmission_costs(n, costs)
    attach_conventional_generators(n, costs, ppl)
    attach_wind_and_solar(n, costs)
    attach_hydro(n, costs, ppl)
    attach_extendable_generators(n, costs, ppl)

    # TODO: Feature to uncomment and debug
    # estimate_renewable_capacities(n)
    # attach_OPSD_renewables(n)

    update_p_nom_max(n)
    add_nice_carrier_names(n)

    n.export_to_netcdf(snakemake.output[0])
