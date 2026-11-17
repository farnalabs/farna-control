Feature: Contribute fixtures to the org library
  Users can create draft fixture contributions and submit them for review.
  The contribution lifecycle is served by `/api/v1/library/contribute`
  (`contributions.py`): a fixture starts as a `draft` and is submitted to the
  `review_queue`; a published contribution can be versioned into a fresh
  draft. (The community-library publish step was removed with the community
  library.)

  Background:
    Given the organisation exists
    And the user is authenticated

  Scenario: Create a draft fixture contribution
    When the user submits a contribution
      """
      {
        "name": "My Test Fixture",
        "slug": "my-test-fixture",
        "fixture_map": {"prompt": "response"}
      }
      """
    Then the response status is 201
    And the response has contribution_status "draft"

  Scenario: Create contribution with missing required fields returns 422
    When the user submits a contribution
      """
      {
        "name": "Incomplete"
      }
      """
    Then the response status is 422

  Scenario: Submit a draft for review
    Given a draft fixture contribution exists
    When the user submits the contribution for review
    Then the response status is 200
    And the response has contribution_status "review_queue"

  Scenario: Submit a non-draft contribution returns 409
    Given a published fixture contribution exists
    When the user submits the contribution for review
    Then the response status is 409

  Scenario: List contributions
    When the user lists the contributions
    Then the response contains a list of contributions

  Scenario: List contributions filtered by status
    When the user lists the contributions filtered by status "draft"
    Then the response contains only draft contributions

  Scenario: Submit a new version
    Given a published fixture contribution exists
    When the user submits a new version of the contribution
      """
      {
        "name": "Updated Fixture",
        "slug": "updated-fixture",
        "fixture_map": {"new": "response"}
      }
      """
    Then the response status is 201
    And the new version has contribution_status "draft"

  Scenario: Submit version on draft original returns 409
    Given a draft fixture contribution exists
    When the user submits a new version of the contribution
      """
      {
        "name": "Nope",
        "slug": "nope",
        "fixture_map": {"a": "b"}
      }
      """
    Then the response status is 409

  Scenario: List versions of a contribution
    Given a published fixture contribution with versions exists
    When the user lists the contribution versions
    Then the response contains a list of versions
