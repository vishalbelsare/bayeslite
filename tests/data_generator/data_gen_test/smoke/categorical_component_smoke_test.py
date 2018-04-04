from tests.data_generator.data_gen_source.categorical.categorical_component import CategoricalComponent
from tests.data_generator.data_gen_test.seed_string_to_int import hash_32_unsigned
from tests.stochastic import stochastic

simulate_sample_size = 10


@stochastic(max_runs=1, min_passes=1)
def test_categorical_component_smoke(seed):
    seed = hash_32_unsigned(seed)
    params = [0.2, 0.3, 0.5]

    cat = CategoricalComponent(params)

    # simulate
    samples = cat.simulate(simulate_sample_size, seed)
    possible = {0, 1, 2}

    for s in samples:
        assert s in possible, "simulated an impossible value"

    # likelihood
    choice = 0
    assert cat.likelihood(choice) == 0.2, "likelihood of " + str(choice) + " incorrect. Expected: " + \
                                          str(params[choice]) + ". Got: " + str(cat.likelihood(choice))

# test_categorical_component_smoke()