import unittest

from firedex_algorithm_experiment import FiredexAlgorithmExperiment


class TestExperimentConfiguration(unittest.TestCase):

    def test_subscriptions(self):
        # only one topic for each class
        exp = FiredexAlgorithmExperiment(num_topics=10, topic_class_weights=(0.5, 0.5), topic_class_sub_rates=(0.2, 0.2),
                                         draw_subscriptions_from_advertisements=False)
        exp.generate_configuration()
        subs = exp.subscriptions

        # ensure we have the right amount for each class
        class0 = list(exp.topics_for_class(0))
        class1 = list(exp.topics_for_class(1))
        self.assertEqual(len([t for t in subs if t in class0]), 1)
        self.assertEqual(len([t for t in subs if t in class1]), 1)

        # half topics for each class
        exp = FiredexAlgorithmExperiment(num_topics=20, topic_class_weights=(0.5, 0.5), topic_class_sub_rates=(0.5, 0.5),
                                         topic_class_sub_dists=({"dist": "uniform", "args": [0, 10]},
                                                                {"dist": "uniform", "args": [0, 10]}),
                                         draw_subscriptions_from_advertisements=False)
        exp.generate_configuration()
        subs = exp.subscriptions

        # ensure we have the right amount for each class
        class0 = list(exp.topics_for_class(0))
        class1 = list(exp.topics_for_class(1))
        self.assertEqual(len([t for t in subs if t in class0]), 5)
        self.assertEqual(len([t for t in subs if t in class1]), 5)

        # all topics for each class
        exp = FiredexAlgorithmExperiment(num_topics=20, topic_class_weights=(0.25, 0.75), topic_class_sub_rates=(1.0, 1.0),
                                         topic_class_sub_dists=({"dist": "uniform", "args": [0, 5]},
                                                                {"dist": "uniform", "args": [0, 15]}),
                                         draw_subscriptions_from_advertisements=False)
        exp.generate_configuration()
        subs = exp.subscriptions

        # ensure we have the right amount for each class
        class0 = list(exp.topics_for_class(0))
        class1 = list(exp.topics_for_class(1))
        self.assertEqual(len([t for t in subs if t in class0]), 5)
        self.assertEqual(len([t for t in subs if t in class1]), 15)

        # try Zipf distribution, which many pub-sub papers say is well-representative
        exp = FiredexAlgorithmExperiment(num_topics=20, topic_class_weights=(0.25, 0.75), topic_class_sub_rates=(0.5, 0.2),
                                         topic_class_sub_dists=({"dist": "zipf", "args": [2, -1]},
                                                                {"dist": "zipf", "args": [2, -1]}),
                                         draw_subscriptions_from_advertisements=False)
        exp.generate_configuration()
        subs = exp.subscriptions

        # ensure we have the right amount for each class
        class0 = list(exp.topics_for_class(0))
        class1 = list(exp.topics_for_class(1))
        self.assertEqual(len([t for t in subs if t in class0]), 2)
        self.assertEqual(len([t for t in subs if t in class1]), 3)

        # TODO: play with class weights and sub rates to check edge cases!

    def test_advertisements(self):
        ntopics = 10
        class1_weight = 0.5
        class2_weight = 0.5

        # define various parameter combinations we'll use to test for edge cases
        ff_all_num_ads = ((0,0), (1,1), (5,0), (1,5))
        iot_all_num_ads = ((0,0), (0,0), (2,4), (0, 2))

        # verify different dists work
        dists_to_test = (({'dist': 'uniform', 'args': [0, int(ntopics*class1_weight)]},
                          {'dist': 'uniform', 'args': [0, int(ntopics*class2_weight)]}),
                         # test that requesting ads with distribution whose upper bound is outside topic range
                         #   (e.g. unbounded) gives us the right ads in each class
                         ({'dist': 'uniform', 'args': [0, 2 * ntopics]},
                          {'dist': 'uniform', 'args': [0, 2 * ntopics]}),
                         ({'dist': 'zipf', 'args': [2, -1]},
                          {'dist': 'exp', 'args': [2]}),
                         )

        for (ff_num_ads, iot_num_ads) in zip(ff_all_num_ads, iot_all_num_ads):
            for dists in dists_to_test:
                exp = FiredexAlgorithmExperiment(num_topics=ntopics, topic_class_weights=(class1_weight, class2_weight),
                                                 topic_class_advertisements_per_ff=ff_num_ads,
                                                 topic_class_advertisements_per_iot=iot_num_ads,
                                                 topic_class_pub_dists=dists)
                try:
                    all_pub_ads = exp.generate_advertisements()
                except ValueError as e:
                    self.assertFalse(True, "ERROR generating pubs (#ffpubs=%s, #iotpubs=%s) with dist (%s)... error: %s" % (ff_num_ads, iot_num_ads, dists, e))

                ff_ads, iot_ads = all_pub_ads
                # print all_pub_ads

                # verify we have the expected #ads i.e. each publisher accounted for
                self.assertEqual(len(ff_ads), exp.num_ffs)
                self.assertEqual(len(iot_ads), exp.num_iots)

                class0 = list(exp.topics_for_class(0))
                class1 = list(exp.topics_for_class(1))

                for pub_class_ads, exp_num_pub_class_ads in zip(all_pub_ads, (ff_num_ads, iot_num_ads)):
                    for this_pub_ads in pub_class_ads.values():
                        # verify right #ads for this publisher
                        self.assertEqual(len(this_pub_ads), sum(exp_num_pub_class_ads))
                        c0_ads = [a for a in this_pub_ads if a in class0]
                        c1_ads = [a for a in this_pub_ads if a in class1]

                        # verify the ads are for the right topics
                        self.assertEqual(len(c0_ads), exp_num_pub_class_ads[0])
                        self.assertEqual(len(c1_ads), exp_num_pub_class_ads[1])

    def test_topic_classes(self):
        """
        Verifies that specifying different-sized lists of topic class parameters doesn't cause errors.
        :return:
        """

        # NOTE: these test cases written assuming the default # topic_classes is 2!

        # generic test case
        exp = FiredexAlgorithmExperiment(num_topics=10, topic_class_weights=(0.5, 0.5), topic_class_sub_rates=(1.0, 1.0),
                                         draw_subscriptions_from_advertisements=False)
        exp.generate_configuration()
        self.assertEqual(len(set(exp.subscriptions)), 10)
        self.assertEqual(exp.ntopic_classes, 2)

        # single class test case should expand these params to 2 topic classes
        exp = FiredexAlgorithmExperiment(num_topics=10, topic_class_weights=(0.5,), topic_class_sub_rates=(1.0,),
                                         draw_subscriptions_from_advertisements=False)
        exp.generate_configuration()
        self.assertEqual(len(set(exp.subscriptions)), 10)
        self.assertEqual(exp.ntopic_classes, 2)

        # 5 class test case should expand other params to 5 topic classes
        exp = FiredexAlgorithmExperiment(num_topics=30, topic_class_weights=[0.2]*5, topic_class_sub_rates=(0.5,),
                                         draw_subscriptions_from_advertisements=False)
        exp.generate_configuration()
        self.assertEqual(len(set(exp.subscriptions)), 15)
        self.assertEqual(exp.ntopic_classes, 5)

    def test_rv_sampling_default_params(self):
        """
        Verify RV-based sampling works when the experiment sets default arguments for the range.
        :return:
        """
        # NOTE: most of this is copied directly from test_advertisements
        ntopics = 10
        class1_weight = 0.5
        class2_weight = 0.5

        # verify we get topics outside [0,1) for uniform and we DO NOT get topic 5 due to lower bound of range in class1
        dists_to_test = (({'dist': 'uniform'},
                          {'dist': 'uniform', 'args': [1]}),
                         # verify zipf gets scaled to include topic 0
                         ({'dist': 'zipf', 'args': [2]},
                          {'dist': 'zipf', 'args': [2, 0]}),
                         )
        ff_all_num_ads = ((0,0), (1,1), (5,0), (1,4))
        iot_all_num_ads = ((5,4), (0,0), (2,4), (0, 2))

        for (ff_num_ads, iot_num_ads) in zip(ff_all_num_ads, iot_all_num_ads):
            for dists in dists_to_test:
                exp = FiredexAlgorithmExperiment(num_topics=ntopics, topic_class_weights=(class1_weight, class2_weight),
                                                 topic_class_advertisements_per_ff=ff_num_ads,
                                                 topic_class_advertisements_per_iot=iot_num_ads,
                                                 topic_class_pub_dists=dists)
                try:
                    all_pub_ads = exp.generate_advertisements()
                except ValueError as e:
                    self.assertFalse(True, "ERROR generating pubs (#ffpubs=%s, #iotpubs=%s) with dist (%s)... error: %s" % (ff_num_ads, iot_num_ads, dists, e))

                ff_ads, iot_ads = all_pub_ads
                # print all_pub_ads

                # verify we have the expected #ads i.e. each publisher accounted for
                self.assertEqual(len(ff_ads), exp.num_ffs)
                self.assertEqual(len(iot_ads), exp.num_iots)

                class0 = list(exp.topics_for_class(0))
                class1 = list(exp.topics_for_class(1))

                for pub_class_ads, exp_num_pub_class_ads in zip(all_pub_ads, (ff_num_ads, iot_num_ads)):
                    for this_pub_ads in pub_class_ads.values():
                        # verify right #ads for this publisher
                        self.assertEqual(len(this_pub_ads), sum(exp_num_pub_class_ads))
                        c0_ads = [a for a in this_pub_ads if a in class0]
                        c1_ads = [a for a in this_pub_ads if a in class1]

                        self.assertTrue(exp.topics_for_class(1)[0] not in c1_ads)

                        # verify the ads are for the right topics
                        self.assertEqual(len(c0_ads), exp_num_pub_class_ads[0])
                        self.assertEqual(len(c1_ads), exp_num_pub_class_ads[1])

    def test_simulator_input_file(self):
        """
        Ensures the JSON file containing parameters that drive the queuing simulator is correct.
        Main check to do here is verify the arrival rates are set according to the #publishers on that topic.
        :return:
        """

        ntopics = 10
        class1_weight = 0.5
        class2_weight = 0.5

        #  TEST 1) rates all 0 when no publisher advertisements
        (ff_num_ads, iot_num_ads) = [[0,0]]*2

        exp = FiredexAlgorithmExperiment(num_topics=ntopics, topic_class_weights=(class1_weight, class2_weight),
                                         topic_class_advertisements_per_ff=ff_num_ads,
                                         topic_class_advertisements_per_iot=iot_num_ads,
                                         draw_subscriptions_from_advertisements=False)

        try:
            exp.setup_experiment()
        except ValueError as e:
            self.assertFalse(True, "ERROR generating pubs (#ffpubs=%s, #iotpubs=%s)... error: %s" % (ff_num_ads, iot_num_ads, e))

        lambdas = exp.get_simulator_input_dict()['lambdas']

        for total_rate, topic_rate in zip(lambdas, exp.pub_rates):
            self.assertEqual(total_rate, 0)

        #  TEST 2) rates all multiplied by npubs when all publishers advertise all topics
        (ff_num_ads, iot_num_ads) = [[5, 5]]*2

        exp = FiredexAlgorithmExperiment(num_topics=ntopics, topic_class_weights=(class1_weight, class2_weight),
                                         topic_class_advertisements_per_ff=ff_num_ads,
                                         topic_class_advertisements_per_iot=iot_num_ads)

        try:
            exp.setup_experiment()
        except ValueError as e:
            self.assertFalse(True, "ERROR generating pubs (#ffpubs=%s, #iotpubs=%s)... error: %s" % (ff_num_ads, iot_num_ads, e))

        lambdas = exp.get_simulator_input_dict()['lambdas']

        for total_rate, topic_rate in zip(lambdas, exp.pub_rates):
            self.assertAlmostEqual(total_rate, topic_rate * exp.npublishers)  # some round-off error!

        #  TEST 3) as an in between test, have only one topic class advertised
        (ff_num_ads, iot_num_ads) = [[5, 0], [0, 5]]

        exp = FiredexAlgorithmExperiment(num_topics=ntopics, topic_class_weights=(class1_weight, class2_weight),
                                         topic_class_advertisements_per_ff=ff_num_ads,
                                         topic_class_advertisements_per_iot=iot_num_ads)

        try:
            exp.setup_experiment()
        except ValueError as e:
            self.assertFalse(True, "ERROR generating pubs (#ffpubs=%s, #iotpubs=%s)... error: %s" % (
            ff_num_ads, iot_num_ads, e))

        lambdas = exp.get_simulator_input_dict()['lambdas']

        for total_rate, topic_rate in zip(lambdas[:exp.ntopics_per_class[0]], exp.pub_rates[:exp.ntopics_per_class[0]]):
            self.assertAlmostEqual(total_rate, topic_rate * exp.num_ffs)  # some round-off error!

        for total_rate, topic_rate in zip(lambdas[exp.ntopics_per_class[0]:], exp.pub_rates[exp.ntopics_per_class[0]:]):
            self.assertAlmostEqual(total_rate, topic_rate * exp.num_iots)  # some round-off error!

    def test_utility_function_weights(self):
        # constant weights
        class_util_weights = (2, 4)
        exp = FiredexAlgorithmExperiment(num_topics=10, topic_class_weights=(0.5, 0.5), topic_class_sub_rates=(0.8, 0.8),
                                         draw_subscriptions_from_advertisements=False,
                                         topic_class_utility_weights=class_util_weights)
        exp.generate_configuration()

        weights = exp._utility_weights
        subs = exp.subscriptions
        c0_weights = [w for w, sub in zip(weights, subs) if exp.class_for_topic(sub) == 0]
        c1_weights = [w for w, sub in zip(weights, subs) if exp.class_for_topic(sub) == 1]
        self.assertEqual(len(c0_weights), 4) # "not enough weights for expected # subscriptions!"
        self.assertEqual(len(c1_weights), 4) # "not enough weights for expected # subscriptions!")
        self.assertTrue(all(class_util_weights[0] == w for w in c0_weights))
        self.assertTrue(all(class_util_weights[1] == w for w in c1_weights))

        for t in range(0, 5):
            self.assertIn(exp.get_utility_weight(t), [0, 2])
        for t in range(5, 10):
            self.assertIn(exp.get_utility_weight(t), [0, 4])

        # ENHANCE: verify it works for some actual distributions... seems to!

        # exp = FiredexAlgorithmExperiment(num_topics=10, topic_class_weights=(0.5, 0.5), topic_class_sub_rates=(0.8, 0.8),
        #                                  draw_subscriptions_from_advertisements=False)
        # exp.generate_configuration()
        #
        # print "WEIGHTS:", exp._utility_weights


if __name__ == '__main__':
    unittest.main()
